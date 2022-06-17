# Defactorization Transformer

## Evaluation
To evaluate on ImageNet val with a single GPU run:
```
bash dist_train.sh configs/devit/devit_b0.py 1 --data-path /data3/QHL/DATA/ImageNet2012/ --data-set IMNET --resume /path/to/checkpoint_file --eval
```

## Training
To train on ImageNet or CIFAR100 on a single node with 8 gpus for 300 epochs run:

```
CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 nohup bash dist_train.sh configs/devit/devit_b0.py 8 --data-path /data3/QHL/DATA/ImageNet2012/ --data-set IMNET > log/devit_b0_imagenet.log 2>&1 &

CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 nohup bash dist_train.sh configs/devit/devit_b0.py 8 --data-path /data3/publicData/cifar100/ --data-set CIFAR > log/devit_b0_cifar.log 2>&1 &
```

## Calculating FLOPS & Params

```
python get_flops.py devit_b0
```