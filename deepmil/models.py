# DeeplabV3:  L.-C. Chen, G. Papandreou, F. Schroff, and H. Adam.  Re-
# thinking  atrous  convolution  for  semantic  image  segmenta-
# tion. arXiv preprint arXiv:1706.05587, 2017..

# Source based: https://github.com/speedinghzl/pytorch-segmentation-toolbox
# BN: https://github.com/mapillary/inplace_abn

# PSPNet:  H. Zhao, J. Shi, X. Qi, X. Wang, and J. Jia.  Pyramid scene
# parsing network. In CVPR, pages 2881–2890, 2017. https://arxiv.org/abs/1612.01105


# Other stuff: https://github.com/pytorch/vision/blob/master/torchvision/models/resnet.py

# Deeplab:
# https://github.com/speedinghzl/pytorch-segmentation-toolbox
# https://github.com/speedinghzl/Pytorch-Deeplab
# https://github.com/kazuto1011/deeplab-pytorch
# https://github.com/isht7/pytorch-deeplab-resnet
# https://github.com/pytorch/vision/blob/master/torchvision/models/resnet.py
# https://github.com/CSAILVision/semantic-segmentation-pytorch


# Pretrained:
# https://github.com/CSAILVision/semantic-segmentation-pytorch/blob/28aab5849db391138881e3c16f9d6482e8b4ab38/dataset.py
# https://github.com/CSAILVision/sceneparsing
# https://github.com/CSAILVision/semantic-segmentation-pytorch/tree/28aab5849db391138881e3c16f9d6482e8b4ab38
# Input normalization (natural images):
# https://github.com/CSAILVision/semantic-segmentation-pytorch/blob/28aab5849db391138881e3c16f9d6482e8b4ab38/dataset.py


import sys
import math
import os
import collections
import numbers

from urllib.request import urlretrieve

import torch
from torch.nn.parameter import Parameter
import torch.nn as nn
from torch.nn import functional as F

sys.path.append("..")

from deepmil.decision_pooling import WildCatPoolDecision, ClassWisePooling

BatchNorm2d = nn.BatchNorm2d
InPlaceABNSync = nn.BatchNorm2d

# DEFAULT SEGMENTATION PARAMETERS ###########################

INNER_FEATURES = 256  # ASPPModule
OUT_FEATURES = 512  # ASPPModule, PSPModule,

#
# ###########################################################

ALIGN_CORNERS = True

__all__ = ['resnet18', 'resnet50', 'resnet101']


model_urls = {
    'resnet18': 'http://sceneparsing.csail.mit.edu/model/pretrained_resnet/resnet18-imagenet.pth',
    'resnet50': 'http://sceneparsing.csail.mit.edu/model/pretrained_resnet/resnet50-imagenet.pth',
    'resnet101': 'http://sceneparsing.csail.mit.edu/model/pretrained_resnet/resnet101-imagenet.pth'
}


def conv3x3(in_planes, out_planes, stride=1):
    """
    3x3 convolution with padding.
    :param in_planes:
    :param out_planes:
    :param stride:
    :return:
    """
    return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride, padding=1, bias=False)


class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self, inplanes, planes, stride=1, downsample=None):
        super(BasicBlock, self).__init__()
        self.conv1 = conv3x3(inplanes, planes, stride)
        self.bn1 = BatchNorm2d(planes)
        self.relu = nn.ReLU(inplace=True)
        self.conv2 = conv3x3(planes, planes)
        self.bn2 = BatchNorm2d(planes)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)

        if self.downsample is not None:
            residual = self.downsample(x)

        out += residual
        out = self.relu(out)

        return out


class Bottleneck(nn.Module):
    expansion = 4

    def __init__(self, inplanes, planes, stride=1, downsample=None):
        super(Bottleneck, self).__init__()
        self.conv1 = nn.Conv2d(inplanes, planes, kernel_size=1, bias=False)
        self.bn1 = BatchNorm2d(planes)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, stride=stride,
                               padding=1, bias=False)
        self.bn2 = BatchNorm2d(planes)
        self.conv3 = nn.Conv2d(planes, planes * 4, kernel_size=1, bias=False)
        self.bn3 = BatchNorm2d(planes * 4)
        self.relu = nn.ReLU(inplace=True)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = self.relu(out)

        out = self.conv3(out)
        out = self.bn3(out)

        if self.downsample is not None:
            residual = self.downsample(x)

        out += residual
        out = self.relu(out)

        return out


class WildCatClassifierHead(nn.Module):
    """
    A WILDCAT type classifier head.
    """
    def __init__(self, inplans, modalities, num_classes, kmax=0.5, kmin=None, alpha=0.6, dropout=0.0):
        super(WildCatClassifierHead, self).__init__()

        self.to_modalities = nn.Conv2d(inplans, num_classes * modalities, kernel_size=1, bias=True)
        self.to_maps = ClassWisePooling(num_classes, modalities)
        self.wildcat = WildCatPoolDecision(kmax=kmax, kmin=kmin, alpha=alpha, dropout=dropout)

    def forward(self, x):
        modalities = self.to_modalities(x)
        maps = self.to_maps(modalities)
        scores = self.wildcat(maps)

        return scores, maps


class ResNet(nn.Module):
    def __init__(self, block, layers, num_masks=1, sigma=0.5, w=8, num_classes=2, scale=(0.5, 0.5),
                 modalities=4, kmax=0.5, kmin=None, alpha=0.6, dropout=0.0, nbr_times_erase=0, sigma_erase=10):
        """
        Init. function.
        :param block: class of the block.
        :param layers: list of int, number of layers per block.
        :param num_masks: int, number of masks to output. (supports only 1).
        """
        assert num_masks == 1, "The model produces only 1 mask (where 1 is the regions of interest and 0 is the " \
                               "background). You asked for `{}` masks .... [NOT OK]".format(num_masks)

        # classifier stuff
        assert isinstance(scale, tuple) or isinstance(scale, list) or isinstance(scale, float), "`scale` should be a " \
                                                                                                "tuple, or a list, " \
                                                                                                "or a float with " \
                                                                                                "values in ]0, " \
                                                                                                "1]. You provided " \
                                                                                                "{} .... [NOT " \
                                                                                                "OK]".format(scale)
        if isinstance(scale, tuple) or isinstance(scale, list):
            assert 0 < scale[0] <= 1, "`scale[0]` (height) should be > 0 and <= 1. You provided `{}` ... [NOT " \
                                      "OK]".format(scale[0])
            assert 0 < scale[0] <= 1, "`scale[1]` (width) should be > 0 and <= 1. You provided `{}` .... [NOT " \
                                      "OK]".format(scale[1])
        elif isinstance(scale, float):
            assert 0 < scale <= 1, "`scale` should be > 0, <= 1. You provided `{}` .... [NOT OK]".format(scale)
            scale = (scale, scale)

        self.scale = scale
        self.num_classes = num_classes
        self.nbr_times_erase = nbr_times_erase
        self.nbr_times_erase_backup = nbr_times_erase
        assert sigma_erase > 0, "`sigma_erase` must be positive float. You provided {} .... [NOT OK]".format(
            sigma_erase)
        self.sigma_erase = float(sigma_erase)

        self.inplanes = 128
        super(ResNet, self).__init__()

        # Encoder

        self.conv1 = conv3x3(3, 64, stride=2)
        self.bn1 = BatchNorm2d(64)
        self.relu1 = nn.ReLU(inplace=True)
        self.conv2 = conv3x3(64, 64)
        self.bn2 = BatchNorm2d(64)
        self.relu2 = nn.ReLU(inplace=True)
        self.conv3 = conv3x3(64, 128)
        self.bn3 = BatchNorm2d(128)
        self.relu3 = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)

        self.layer1 = self._make_layer(block, 64, layers[0])
        self.layer2 = self._make_layer(block, 128, layers[1], stride=2)
        self.layer3 = self._make_layer(block, 256, layers[2], stride=2)
        self.layer4 = self._make_layer(block, 512, layers[3], stride=2)

        # Find out the size of the output.

        if isinstance(self.layer4[-1], Bottleneck):
            in_channel4 = self.layer1[-1].bn3.weight.size()[0]
            in_channel8 = self.layer2[-1].bn3.weight.size()[0]
            in_channel16 = self.layer3[-1].bn3.weight.size()[0]
            in_channel32 = self.layer4[-1].bn3.weight.size()[0]
        elif isinstance(self.layer4[-1], BasicBlock):
            in_channel4 = self.layer1[-1].bn2.weight.size()[0]
            in_channel8 = self.layer2[-1].bn2.weight.size()[0]
            in_channel16 = self.layer3[-1].bn2.weight.size()[0]
            in_channel32 = self.layer4[-1].bn2.weight.size()[0]
        else:
            raise ValueError("Supported class .... [NOT OK]")

        print(in_channel32, in_channel16, in_channel8, in_channel4)

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, math.sqrt(2. / n))
            elif isinstance(m, BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()

        # =========================================  SEGMENTOR =========================================
        self.conts1 = torch.tensor([sigma], requires_grad=False).float()
        self.register_buffer("sigma", self.conts1)

        self.const2 = torch.tensor([w], requires_grad=False).float()
        self.register_buffer("w", self.const2)

        self.headseg = WildCatClassifierHead(in_channel32, modalities, num_classes, kmax=kmax, kmin=kmin, alpha=alpha,
                                             dropout=dropout)

        print("Num. parameters up32: {}".format(sum([p.numel() for p in self.headseg.parameters()])))
        # =============================================================================================

        # ======================================== CLASSIFIER ==========================================
        self.cl32 = WildCatClassifierHead(in_channel32, modalities, num_classes, kmax=kmax, kmin=kmin, alpha=alpha,
                                          dropout=dropout)
        # self.cl16 = WildCatClassifierHead(in_channel16, modalities, num_classes, kmax=kmax, kmin=kmin, alpha=alpha,
        #                                  dropout=dropout)
        # ==============================================================================================

    def _make_layer(self, block, planes, blocks, stride=1, dilation=1, multi_grid=1):
        downsample = None
        if stride != 1 or self.inplanes != planes * block.expansion:
            downsample = nn.Sequential(
                nn.Conv2d(self.inplanes, planes * block.expansion,
                          kernel_size=1, stride=stride, bias=False),
                BatchNorm2d(planes * block.expansion)
            )

        layers = []
        layers.append(block(self.inplanes, planes, stride, downsample))
        self.inplanes = planes * block.expansion
        for i in range(1, blocks):
            layers.append(block(self.inplanes, planes))

        return nn.Sequential(*layers)

    def forward(self, x):
        """
        Forward function.
        :param x:
        :return:
        """
        # 1. Segment: forward.
        mask, scores_seg, maps_seg = self.segment(x)  # mask is detached!

        mask, x_pos, x_neg = self.get_mask_xpos_xneg(x, mask)

        # 3. Classify.
        out_pos = self.classify(x_pos)
        out_neg = self.classify(x_neg)

        return out_pos, out_neg, mask, scores_seg, maps_seg

    def get_mask_xpos_xneg(self, x, mask_c):
        """
        Compute X+, X-.
        :param x: input X.
        :param mask_c: continous mask.
        :return:
        """
        # 2. Prepare the mask for multiplication.
        mask = self.get_pseudo_binary_mask(mask_c)  # detached!
        x_pos, x_neg = self.apply_mask(x, mask)  # detached!

        return mask, x_pos, x_neg

    def segment(self, x):
        """
        Forward function.
        Any mask is is composed of two 2D plans:
            1. The first plan represents the background.
            2. The second plan represents the regions of interest (glands).

        :param x: tensor, input image with size (nb_batch, depth, h, w).
        :return: (out_pos, out_neg, mask):
            x_pos: tensor, the image with the mask applied. size (nb_batch, depth, h, w)
            x_neg: tensor, the image with the complementary mask applied. size (nb_batch, depth, h, w)
            out_pos: tuple of tensors, the output of the classification of the positive regions. (scores,
            wildcat feature maps)
            out_neg: tuple of tensors, the output of the classification of the negative regions. (scores,
            wildcat feature maps).
            mask: tensor, the mask of each image in the batch. size (batch_size, 1, h, w) if evaluation mode is on or (
            batch_size, 1, h*, w*) where h*, w* is the size of the input after downsampling (if the training mode is
            on).
        """
        b, _, h, w = x.shape

        # x: 1 / 1: [n, 3, 480, 480]
        # Only number of filters change: (18, 50, 101): a/b/c.
        # Down-sample:
        x_0 = self.relu1(self.bn1(self.conv1(x)))  # 1 / 2: [n, 64, 240, 240]   --> x2^1 to get back to 1. (
        # downsampled due to the stride=2)
        x_1 = self.relu2(self.bn2(self.conv2(x_0)))  # 1 / 2: [n, 64, 240, 240]   --> x2^1 to get back to 1.
        x_2 = self.relu3(self.bn3(self.conv3(x_1)))  # 1 / 2: [2, 128, 240, 240]  --> x2^1 to get back to 1.
        x_3 = self.maxpool(x_2)        # 1 / 4:  [2, 128, 120, 120]         --> x2^2 to get back to 1.
        x_4 = self.layer1(x_3)       # 1 / 4:  [2, 64/256/--, 120, 120]   --> x2^2 to get back to 1.
        x_8 = self.layer2(x_4)     # 1 / 8:  [2, 128/512/--, 60, 60]    --> x2^3 to get back to 1.
        x_16 = self.layer3(x_8)    # 1 / 16: [2, 256/1024/--, 30, 30]   --> x2^4 to get back to 1.
        # x_16 = F.dropout(x_16, p=0.3, training=self.training, inplace=False)
        x_32 = self.layer4(x_16)   # 1 / 32: [n, 512/2048/--, 15, 15]   --> x2^5 to get back to 1.

        scores, maps = self.headseg(x_32)

        # compute M+
        prob = F.softmax(scores, dim=1)
        mpositive = torch.zeros((b, 1, maps.size()[2], maps.size()[3]), dtype=maps.dtype, layout=maps.layout,
                                device=maps.device)
        for i in range(b):  # for each sample
            for j in range(prob.size()[1]):  # sum the: prob(class) * mask(class)
                mpositive[i] = mpositive[i] + prob[i, j] * maps[i, j, :, :]

        mpos_inter = F.interpolate(input=mpositive, size=(h, w), mode='bilinear', align_corners=ALIGN_CORNERS).detach()

        return mpos_inter, scores, maps

    def classify(self, x):
        # Resize the image first.
        _, _, h, w = x.shape
        h_s, w_s = int(h * self.scale[0]), int(w * self.scale[1])
        # reshape
        x = F.interpolate(input=x, size=(h_s, w_s), mode='bilinear', align_corners=ALIGN_CORNERS)

        x = self.relu1(self.bn1(self.conv1(x)))  # 1 / 2: [n, 64, 240, 240]   --> x2^1 to get back to 1.
        x = self.relu2(self.bn2(self.conv2(x)))  # 1 / 2: [n, 64, 240, 240]   --> x2^1 to get back to 1.
        x = self.relu3(self.bn3(self.conv3(x)))  # 1 / 2: [2, 128, 240, 240]  --> x2^1 to get back to 1.
        x = self.maxpool(x)  # 1 / 4:  [2, 128, 120, 120]         --> x2^2 to get back to 1.
        x_4 = self.layer1(x)  # 1 / 4:  [2, 64/256/--, 120, 120]   --> x2^2 to get back to 1.
        x_8 = self.layer2(x_4)  # 1 / 8:  [2, 128/512/--, 60, 60]    --> x2^3 to get back to 1.
        x_16 = self.layer3(x_8)  # 1 / 16: [2, 256/1024/--, 30, 30]   --> x2^4 to get back to 1.
        x_32 = self.layer4(x_16)  # 1 / 32: [n, 512/2048/--, 15, 15]   --> x2^5 to get back to 1.

        # classifier at 32.
        scores32, maps32 = self.cl32(x_32)

        # Final
        scores, maps = scores32, maps32

        return scores, maps

    def get_pseudo_binary_mask(self, x):
        """
        Compute a mask by applying a sigmoid function.
        The mask is not binary but pseudo-binary (values are close to 0/1).

        :param x: tensor of size (batch_size, 1, h, w), contain the feature map representing the mask.
        :return: tensor, mask. with size (nbr_batch, 1, h, w).
        """
        x = (x - x.min()) / (x.max() - x.min())
        return torch.sigmoid(self.w * (x - self.sigma))

    def apply_mask(self, x, mask):
        """
        Apply a mask (and its complement) over an image.

        :param x: tensor, input image. [size: (nb_batch, depth, h, w)]
        :param mask: tensor, mask. [size, (nbr_batch, 1, h, w)]
        :return: (x_pos, x_neg)
            x_pos: tensor of size (nb_batch, depth, h, w) where only positive regions are shown (the negative regions
            are set to zero).
            x_neg: tensor of size (nb_batch, depth, h, w) where only negative regions are shown (the positive regions
            are set to zero).
        """
        mask_expd = mask.expand_as(x)
        x_pos = x * mask_expd
        x_neg = x * (1 - mask_expd)

        return x_pos, x_neg


def load_url(url, model_dir='../pretrained', map_location=torch.device('cpu')):
    """
    Download pre-trained models.
    :param url: str, url of the pre-trained model.
    :param model_dir: str, path to the temporary folder where the pre-trained models will be saved.
    :param map_location: a function, torch.device, string, or dict specifying how to remap storage locations.
    :return: torch.load() output. Loaded dict state.
    """
    if not os.path.exists(model_dir):
        os.makedirs(model_dir)
    filename = url.split('/')[-1]
    cached_file = os.path.join(model_dir, filename)
    if not os.path.exists(cached_file):
        sys.stderr.write('Downloading: "{}" to {}\n'.format(url, cached_file))
        urlretrieve(url, cached_file)
    return torch.load(cached_file, map_location=map_location)


def resnet18(pretrained=False, **kwargs):
    """Constructs a ResNet-18 model.
    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
    """
    model = ResNet(BasicBlock, [2, 2, 2, 2], **kwargs)
    if pretrained:
        model.load_state_dict(load_url(model_urls['resnet18']), strict=False)
    return model


def resnet50(pretrained=False, **kwargs):
    """Constructs a ResNet-50 model.
    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
    """
    model = ResNet(Bottleneck, [3, 4, 6, 3], **kwargs)
    if pretrained:
        model.load_state_dict(load_url(model_urls['resnet50']), strict=False)
    return model


def resnet101(pretrained=False, **kwargs):
    """Constructs a ResNet-101 model.
    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
    """
    model = ResNet(Bottleneck, [3, 4, 23, 3], **kwargs)
    if pretrained:
        model.load_state_dict(load_url(model_urls['resnet101']), strict=False)
    return model


def test_resnet():
    model = resnet18(pretrained=True, num_masks=1, head=None)
    print("Testing {}".format(model.__class__.__name__))
    model.train()
    print("Num. parameters: {}".format(sum([p.numel() for p in model.parameters()])))
    cuda = "1"
    print("cuda:{}".format(cuda))
    print("DEVICE BEFORE: ", torch.cuda.current_device())
    DEVICE = torch.device("cuda:{}".format(cuda) if torch.cuda.is_available() else "cpu")
    if torch.cuda.is_available():
        # torch.cuda.set_device(int(cuda))
        pass

    print("DEVICE AFTER: ", torch.cuda.current_device())
    # DEVICE = torch.device("cpu")
    model.to(DEVICE)
    x = torch.randn(2, 3, 480, 480)
    x = x.to(DEVICE)
    out_pos, out_neg, mask = model(x)
    print(x.size(), mask.size())


if __name__ == "__main__":
    import sys

    # test_resnet()
