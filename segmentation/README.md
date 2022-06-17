# Applying DeViT to Semantic Segmentation

## Evaluation
To evaluate on a single node with 8 gpus run:
```
dist_test.sh configs/sem_fpn/DeViT/fpn_devit_b0_ade20k_40k.py /path/to/checkpoint_file 8 --out results.pkl --eval mIoU
```

## Training
To train on a single node with 8 gpus run:

```
CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 nohup bash dist_train.sh configs/sem_fpn/DeViT/fpn_devit_b0_ade20k_40k.py 8 > log/DeViT/fpn_devit_b0_ade20k_40k.log 2>&1 &
```
