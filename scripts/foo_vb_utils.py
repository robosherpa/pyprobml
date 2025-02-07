import numpy as np

import jax
import jax.numpy as jnp
from jax import random, vmap, tree_map

from flax.core.frozen_dict import unfreeze, freeze
from flax import traverse_util


def gen_phi(key, w_mat_lst):
    # TODO : Make it work with features list
    phi = {}

    for k, v in w_mat_lst.items():
        normal_key, key = random.split(key)
        phi[k] = random.normal(normal_key, shape=v.shape)
    
    return phi

def update_weight(params, w_mat_lst):
    """
        This function update the parameters of the network.
        :param tensor_lst: A list of iterators of the network parameters.
        :param w_mat_lst: A list of matrices in size of P*N.
        :return:
    """
    params = unfreeze(params)
    updated_params = {}
    for i, (k, v) in enumerate(traverse_util.flatten_dict(params).items()):
        
        if k[-1] == 'kernel':
            updated_params[k] = w_mat_lst[k][:, :-1]
            updated_params[tuple([*k[:-1], 'bias'])] = w_mat_lst[k][:, -1]
    updated_params = freeze(traverse_util.unflatten_dict(updated_params))
    return updated_params

def randomize_weights(params, w_mat_lst, m_mat_lst, a_mat_lst, b_mat_lst, phi_mat_lst):
    """
        This function generate a sample of normal random weights with mean M and covariance matrix of (A*A^t)\otimes(B*B^t)
        (\otimes = kronecker product). In matrix form the update rule is W = M + B*Phi*A^t.
        :param tensor_lst: A list of iterators of the network parameters.
        :param w_mat_lst: m_mat_lst: A list of matrices in size of P*N.
        :param m_mat_lst: m_mat_lst: A list of matrices in size of P*N.
        :param a_mat_lst: A list of matrices in size of N*N.
        :param b_mat_lst: A list of matrices in size of P*P.
        :param phi_mat_lst: A list of normal random matrices in size of P*N.
        :return:
    """
    # W = M + B*Phi*A^t

    w_mat_lst = jax.tree_multimap(lambda m, b, phi, a: m + ((b @ phi) @ a.T ),
                                  m_mat_lst, b_mat_lst, phi_mat_lst, a_mat_lst)
    params = update_weight(params, w_mat_lst)

def cross_entropy_loss(params, inputs, labels, num_classes, predict_fn):
  logits = predict_fn(params, inputs)
  one_hot_labels = jax.nn.one_hot(labels, num_classes=num_classes)
  xentropy = (logits * one_hot_labels).sum(axis=-1)
  return xentropy.mean()


def weight_grad(grads):
    """
        This function return a list of matrices containing the gradient of the network parameters for each layer.
        :param tensor_lst: A list of iterators of the network parameters.
        :param device: device index to select.
        :return: grad_mat_lst: A list of matrices containing the gradients of the network parameters.
    """
    grad_mat = {}
    grads = unfreeze(grads)

    for k, v  in traverse_util.flatten_dict(grads).items():
        if k[-1] == 'kernel':
          grad_mat[k] = jnp.vstack([v, grad_mat[k]]).T
        else:
          k = (*k[:-1], "kernel")
          grad_mat[k] = v.reshape((1, -1))


    grads = freeze(grads)
    return grad_mat

def create_random_perm(key, n_permutations):
    """
        This function returns a list of array permutation (size of 28*28 = 784) to create permuted MNIST data.
        Note the first permutation is the identity permutation.
        :param n_permutations: number of permutations.
        :return perm_lst: a list of permutations.
    """
    initial_array = jnp.arange(784)
    keys = random.split(key, n_permutations-1)
    
    def permute(key):
      return random.permutation(key, initial_array)

    permutation_list = vmap(permute)(keys)
    return jnp.vstack([initial_array, permutation_list])

def aggregate_grads(avg_psi_mat_lst, grad_mat_list, train_mc_iters):
    """
        This function estimate the expectation of the gradient using Monte Carlo average.
        :param args: Training settings.
        :param avg_psi_mat_lst: A list of matrices in size of P*N.
        :param grad_mat_list: A list of matrices in size of P*N.
        :return:
    """
    for k, v in grad_mat_list.items():
        avg_psi_mat_lst[k] += (1/train_mc_iters)*v

def aggregate_e_a(e_a_mat_lst, grads, b_mat_lst, phi_mat_lst, train_mc_iters):
    """
        This function estimate the expectation of the e_a ((1/P)E(Psi^t*B*Phi)) using Monte Carlo average.
        :param args: Training settings.
        :param e_a_mat_lst: A list of matrices in size of N*N.
        :param grad_mat_lst: A list of matrices in size of P*N.
        :param b_mat_lst: A list of matrices in size of P*P.
        :param phi_mat_lst: A list of normal random matrices in size of P*N.
        :return:
    """
    for k, v in grads.items():
        b, phi = b_mat_lst[k], phi_mat_lst[k]
        e_a_mat_lst[k] += (1/(train_mc_iters * b.shape[0])) * ((v.T @  b) @ phi)

def aggregate_e_b(e_b_mat_lst, grads, a_mat_lst, phi_mat_lst, train_mc_iters):
    """
        This function estimate the expectation of the e_b ((1/N)E(Phi^t*A*Psi)) using Monte Carlo average.
        :param args: Training settings.
        :param e_b_mat_lst: A list of matrices in size of P*P.
        :param grad_mat_lst: A list of matrices in size of P*N.
        :param a_mat_lst: A list of matrices in size of N*N.
        :param phi_mat_lst: A list of normal random matrices in size of P*N
        :return:
    """
    for k, v in grads.items():
        a, phi = a_mat_lst[k], phi_mat_lst[k]
        e_b_mat_lst[k] += (1/(train_mc_iters * a.shape[0])) *((v @ a) @  phi.T)

def update_m(m_mat_lst, a_mat_lst, b_mat_lst, avg_psi_mat_lst, eta=1, diagonal=False):
    """
        This function updates the mean according to M = M - B*B^t*E[Psi]*A*A^t.
        :param m_mat_lst: m_mat_lst: A list of matrices in size of P*N.
        :param a_mat_lst: A list of matrices in size of N*N.
        :param b_mat_lst: A list of matrices in size of P*P.
        :param avg_psi_mat_lst: A list of matrices in size of P*N.
        :param eta: .
        :param diagonal: .
        :return:
    """
    if diagonal:
        # M = M - diag(B*B^t)*E[Psi]*diag(A*A^t)
        m_mat_lst = jax.tree_multimap(lambda m, b, avg, a: m -eta +
                                         (jnp.diag(jnp.diag(b @ b.T)) @ avg) @ jnp.diag(jnp.diag(a @ a.T)
                                         ),
                                 m_mat_lst, b_mat_lst, avg_psi_mat_lst, a_mat_lst)
    else:
        # M = M - B*B^t*E[Psi]*A*A^t
        m_mat_lst = jax.tree_multimap(lambda m, b, avg, a: m -eta+ ((b @ b.T) @ avg)@ (a @ a.T),
                              m_mat_lst, b_mat_lst, avg_psi_mat_lst, a_mat_lst) 


def solve_matrix_equation(v_mat, e_mat, print_norm_flag=False):
    """
        This function returns a solution for the following non-linear matrix equation XX^{\top}+VEX^{\top}-V = 0.
        All the calculations are done in double precision.
        :param v_mat: N*N PD matrix.
        :param e_mat: N*N matrix.
        :param print_norm_flag: Boolean parameter. Print the norm of the matrix equation.
        :return: x_mat: N*N matrix a solution to the non-linear matrix equation.
    """
    # B = V + (1/4)V*E*(E^T)*V
    #v = jax.tree_map(lambda x: x.astype(jnp.float64), v)
    #e = jax.tree_map(lambda x: x.astype(jnp.float64), e)

    v_mat = jnp.vstack(jax.tree_leaves(v_mat))
    e_mat = jnp.vstack(jax.tree_leaves(e_mat))

    ve_product = v_mat @ e_mat

    b_mat = v_mat + 0.25 + (ve_product @ ve_product.T)
    left_mat, diag_mat, right_mat = jnp.linalg.svd(b_mat)

    # ??? : assert (jnp.min(diag_mat) > 0), "v_mat is singular!"

    # L = B^{1/2}
    l_mat = ((left_mat @ jnp.diag(jnp.sqrt(diag_mat))) @
                   right_mat.T)
    inv_l_mat = (right_mat @ jnp.diag(1 / jnp.sqrt(diag_mat))) @ left_mat.T
    # L^-1*V*E=S*Lambda*W^t (SVD)
    s_mat, lambda_mat, w_mat = jnp.linalg.svd(inv_l_mat @ ve_product)
    # Q = S*W^t
    q_mat = s_mat @ w_mat.T
    # X = L*Q-(1/2)V*E
    x_mat = (l_mat @ q_mat) - 0.5 +  ve_product
    ''' TODO : if print_norm_flag:
        mat = torch.add(torch.add(torch.mm(x_mat, torch.transpose(x_mat, 0, 1)),
                                  torch.mm(ve_product, torch.transpose(x_mat, 0, 1))), -1, v_mat)
        mat_norm = torch.norm(mat)
        print('The Frobenius norm of the matrix is', mat_norm.item())'''
    return x_mat.astype(jnp.float64)

def update_a_b(a_mat_lst, b_mat_lst, e_a_mat_lst, e_b_mat_lst, use_gsvd = False):
    """
        This function updates the matrices A & B using a solution to the non-linear matrix equation
        XX^{\top}+VEX^{\top}-V = 0.
        :param a_mat_lst:
        :param b_mat_lst:
        :param e_a_mat_lst:
        :param e_b_mat_lst:
        :return:
    """
    updated_a_mat_lst, updated_b_mat_lst = {}, {}
    for k, a in a_mat_lst.items():
        b = b_mat_lst[k]
        e_a = e_a_mat_lst[k]
        e_b = e_b_mat_lst[k]

        updated_a = solve_matrix_equation(a @ a.T, e_a)
        updated_b = solve_matrix_equation(b @ b.T, e_b)
        
        updated_a_mat_lst[k] = (updated_a)
        updated_b_mat_lst[k] = (updated_b)
    
    a_mat_lst = updated_a_mat_lst
    b_mat_lst = updated_b_mat_lst

def zero_matrix(avg_psi_mat_lst, e_a_mat_lst, e_b_mat_lst):
    """
        :param avg_psi_mat_lst: A list of matrices in size of P*N.
        :param e_a_mat_lst: A list of matrices in size of N*N.
        :param e_b_mat_lst: A list of matrices in size of P*P.
        :return:
    """
    avg_psi_mat_lst = jax.tree_map(jnp.zeros_like, avg_psi_mat_lst)
    e_a_mat_lst = jax.tree_map(jnp.zeros_like, e_a_mat_lst)
    e_b_mat_lst = jax.tree_map(jnp.zeros_like, e_b_mat_lst)

def init_param(key, params, s_init, use_custom_init=False, alpha=0.5):
  """
      :param params: A list of iterators of the network parameters.
      :param s_init: Init value of the diagonal of a and b.
      :return: w_mat_lst: A list of matrices in size of P*N.
      :return: m_mat_lst: A list of matrices in size of P*N.
      :return: a_mat_lst: A list of matrices in size of N*N.
      :return: b_mat_lst: A list of matrices in size of P*P.
      :return: avg_psi_mat_lst: A list of matrices in size of P*N.
      :return: e_a_mat_lst: A list of matrices in size of N*N.
      :return: e_b_mat_lst: A list of matrices in size of P*P.
  """


  w_mat_lst = {}
  m_mat_lst = {}
  a_mat_lst = {}
  b_mat_lst = {}
  avg_psi_mat_lst = {}
  e_a_mat_lst = {}
  e_b_mat_lst = {}

  # Unfreeze params to normal dict.
  params = unfreeze(params)

  for k, v in traverse_util.flatten_dict(params).items():
    if k[-1] =='kernel':
      in_feature, out_feature = v.shape

      w_mat = jnp.zeros((in_feature, out_feature + 1))
      w_mat_lst[k] = w_mat
          
      avg_psi_mat = jnp.zeros((in_feature, out_feature + 1))
      avg_psi_mat_lst[k] = avg_psi_mat


      if use_custom_init:
          m_key, key = random.split(key)
          m_mat = jnp.sqrt((2.0 * alpha / (out_feature + 2.0))) * random.normal(m_key, shape=(in_feature, out_feature + 1))

          coef = jnp.sqrt(jnp.sqrt((2.0 * (1.0 - alpha)/(out_feature + 2.0))))
          a_mat = jnp.diag(coef * jnp.ones((out_feature + 1,)))
          b_mat = jnp.diag(coef * jnp.ones((in_feature, )))
      else:
          key1, key2, key = random.split(key)
          m_mat = jnp.hstack([jnp.sqrt(2.0 / (in_feature + out_feature)) *
                                      random.normal(key1, shape=(in_feature, out_feature)),
                                      jnp.sqrt(2.0/(1.0 + out_feature)) *
                                      random.normal(key2, shape=(in_feature, 1))])
          
          a_mat = jnp.diag(s_init * jnp.ones((out_feature+1, )))
          b_mat = jnp.diag(s_init * jnp.ones((in_feature, )))

      e_a_mat = jnp.zeros((v.shape[1] + 1, v.shape[1]+1))
      e_b_mat = jnp.zeros((v.shape[0], v.shape[0]))

      m_mat_lst[k] = m_mat
      a_mat_lst[k] = a_mat
      b_mat_lst[k] = b_mat
      e_a_mat_lst[k] = e_a_mat
      e_b_mat_lst[k] = e_b_mat

  params = freeze(params)
  return w_mat_lst, m_mat_lst, a_mat_lst, b_mat_lst, avg_psi_mat_lst, e_a_mat_lst, e_b_mat_lst