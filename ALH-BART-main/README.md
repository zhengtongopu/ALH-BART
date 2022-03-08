## Getting Started


### Requirements
* Python 3.6 or higher
* Pytorch >= 1.3.0
* Anaconda

#### Install the transformers

```
cd transformers

pip install --editable ./
```

#### Install the experiment environment

```
pip install -r requirements.txt

pip install wandb
```

### Downloading the data
Please download the dataset and put them in the data folder [here](https://drive.google.com/drive/folders/1NLXjyojklp8uT6U-Ytd-n9qSKRO8Jxz2?usp=sharing)

### Training models

#### Training and Evaluating ALH-BART model with no share
Please run `./src/train_no_share.sh` to train the ALH-BART models with no share.

#### Training and Evaluating ALH-BART model with share
Please run `./src/train_share.sh` to train the ALH-BART models with share.
