_base_ = './fpn_devit_b2_ade20k_40k.py'

# model settings
model = dict(
    pretrained='pretrained/devit_b1.pth',
    backbone=dict(
        type='devit_b1',
        style='pytorch'),
    neck=dict(in_channels=[64, 128, 256, 512]))
