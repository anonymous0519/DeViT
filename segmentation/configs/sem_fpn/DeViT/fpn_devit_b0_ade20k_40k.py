_base_ = './fpn_devit_b2_ade20k_40k.py'

# model settings
model = dict(
    pretrained='pretrained/devit_b0.pth',
    backbone=dict(
        type='devit_b0',
        style='pytorch'),
    neck=dict(in_channels=[32, 64, 128, 256]))
