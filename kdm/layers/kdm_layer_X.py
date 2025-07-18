import keras
import numpy as np
from sklearn.metrics import pairwise_distances
import tensorflow_probability as tfp
from sklearn.neighbors import NearestNeighbors
import tensorflow as tf

def l1_loss(vals):
    '''
    Calculate the l1 loss for a batch of vectors
    Arguments:
        vals: tensor with shape (b_size, n)
    '''
    b_size = keras.ops.cast(keras.ops.shape(vals)[0], dtype=keras.float32)
    vals = keras.utils.normalize(vals, order=2, axis=1)
    loss = keras.ops.sum(keras.ops.abs(vals)) / b_size
    return loss

class KDMLayer(keras.layers.Layer):
    """Kernel Density Matrix Layer
    Receives as input a KDM represented by a set of vectors
    and weight values. 
    Returns a resulting KDM.
    Input shape:
        (batch_size, n_comp_in, dim_x + 1)
        where dim_x is the dimension of the input state
        and n_comp_in is the number of components of the input KDM. 
        The weights of the input KDM of sample i are [i, :, 0], 
        and the components are [i, :, 1:dim_x + 1].
    Output shape:
        (batch_size, n_comp, dim_x)
        where 
            dim_x: the dimension of the output state
            n_comp: the number of components of the joint KDM
        The weights of the output KDM for sample i are [i, :, 0], 
        and the components are [i, :, 1:dim_x + 1].
    Arguments:
        dim_x: int. the dimension of the input state
        x_train: bool. Whether to train or not the x compoments of the joint KDM.
        w_train: bool. Whether to train or not the weights of the joint KDM.
        n_comp: int. Number of components used to represent the joint KDM.
        l1_act: float. Coefficient of the regularization term penalizing the l1
                       norm of the activations.
        l1_x: float. Coefficient of the regularization term penalizing the l1
                       norm of the x components.
    """
    def __init__(
            self,
            kernel,
            dim_x: int,
            x_train: bool = True,
            w_train: bool = True,
            n_comp: int = 0, 
            l1_x: float = 0.,
            l1_act: float = 0.,
            generative: float = 0.,              
            **kwargs
    ):
        super().__init__(**kwargs)
        self.kernel = kernel
        self.dim_x = dim_x
        self.x_train = x_train
        self.w_train = w_train
        self.n_comp = n_comp
        self.l1_x = l1_x
        self.l1_act = l1_act
        self.generative = generative
        self.c_x = self.add_weight(
            shape=(self.n_comp, self.dim_x),
            #initializer=keras.initializers.orthogonal(),
            initializer=keras.initializers.random_normal(),
            trainable=self.x_train,
            name="c_x")
        self.c_w = self.add_weight(
            shape=(self.n_comp,),
            initializer=keras.initializers.constant(1./self.n_comp),
            trainable=self.w_train,
            name="c_w") 
        self.eps = keras.config.epsilon()

    def call(self, inputs):        
        # Weight regularizers
        if self.l1_x != 0:
            self.add_loss(self.l1_x * l1_loss(self.c_x))
            
        comp_w = keras.ops.abs(self.c_w)
        # normalize comp_w to sum to 1
        comp_w_sum = keras.ops.clip(keras.ops.sum(comp_w), 
                                    self.eps, np.inf)
        # comp_w = comp_w / comp_w_sum
        self.c_w.assign(comp_w / comp_w_sum)
        in_w = inputs[:, :, 0]  # shape (b, n_comp_in)
        in_v = inputs[:, :, 1:] # shape (b, n_comp_in, dim_x)
        out_vw = self.kernel(in_v, self.c_x)  # shape (b, n_comp_in, n_comp)
        out_w = self.c_w[np.newaxis, np.newaxis, :] * keras.ops.square(out_vw)
        if self.generative != 0:
            proj = keras.ops.einsum('...i,...ij->...', in_w, out_w) # shape (b, n_comp)
            log_probs = (keras.ops.log(proj + self.eps)
                     + self.kernel.log_weight())
            self.add_loss(-self.generative * keras.ops.mean(log_probs)) 
        out_w = keras.ops.maximum(out_w, self.eps) 
        out_w_sum = keras.ops.sum(out_w, axis=2, keepdims=True) # shape (b, n_comp_in, 1)
        out_w = out_w / out_w_sum # shape (b, n_comp_in, n_comp)
        out_w = keras.ops.einsum('...i,...ij->...j', in_w, out_w) # shape (b, n_comp)
        if self.l1_act != 0:
            self.add_loss(self.l1_act * l1_loss(out_w))
        out_w = keras.ops.expand_dims(out_w, axis=-1) # shape (b, n_comp, 1)
        b_size = keras.ops.shape(out_w)[0]
        out_x = keras.ops.broadcast_to(self.c_x[np.newaxis, ...], 
                                       [b_size, self.n_comp, self.dim_x])
        out = keras.ops.concatenate((out_w, out_x), 2)
        return out

    
    def init_components(self, samples_x, init_sigma=False, sigma_mult=1):
        if init_sigma:
            nn_model = NearestNeighbors(n_neighbors=3)
            nn_model.fit(samples_x)
            distances, _ = nn_model.kneighbors(samples_x)
            sigma = np.mean(distances[:, 2]) * sigma_mult
            self.kernel.sigma.assign(sigma)
        self.c_x.assign(samples_x)
        self.c_w.assign(keras.ops.ones((self.n_comp,)) / self.n_comp)

    def get_config(self):
        config = {
            "dim_x": self.dim_x,
            "n_comp": self.n_comp,
            "x_train": self.x_train,
            "w_train": self.w_train,
            "l1_x": self.l1_x,
            "l1_act": self.l1_act,
        }
        base_config = super().get_config()
        return {**base_config, **config}
    
    def get_distrib(self):
        comp_w = keras.ops.abs(self.c_w) + self.eps
        comp_w = comp_w / keras.ops.sum(comp_w)
        gm = tfp.distributions.MixtureSameFamily(
            reparameterize=True,
            mixture_distribution=tfp.distributions.Categorical(
                                    probs=comp_w),
            components_distribution=tfp.distributions.Independent( 
                tfp.distributions.Normal(
                    loc=self.c_x,  # component 2
                    scale=self.kernel.sigma/np.sqrt(2).astype(np.float32)),
                    reinterpreted_batch_ndims=1))
        return gm

  
