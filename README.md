# Flow Annealed Importance Sampling Bootstrap (FAB)
See corresponding paper [here](https://arxiv.org/abs/2111.11510).

## Methods of Installation

The  package can be installed via pip by navigating in the repository directory and running

```
pip install --upgrade .
```

## Examples
See [double well boltzmann distribution notebook](examples/double_well.ipynb) and [GMM 
target distribution notebook](examples/GMM.ipynb)
for visualised examples of training a normalising flow model.
TBD: further description and plots for examples


## About the code 
The main FAB loss can be found in [core.py](fab/core.py), and we provide a simple training loop to 
train a flow with this loss (or other flow - loss combinations that meet the spec) in [train.py](fab/train.py) 


### Normalizing Flow Libraries
We offer a simple wrapper that allows for various normalising flow libraries to be plugged into 
this repository. The main library we rely on is 
[Normflow](github.com/VincentStimper/normalizing-flows), however we also supply wrappers for 
[nflows](https://github.com/bayesiains/nflows) and flowtorch (TBD). 