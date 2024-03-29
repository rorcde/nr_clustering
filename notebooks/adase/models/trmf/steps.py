# stripped version of `trmf_v0.8_sync.ipynb` as of 2018:08:24 13:12

import numpy as np
import numba as nb

from sklearn.utils.extmath import safe_sparse_dot

import scipy.sparse as sp


from .tron import tron


# In[13]:
@nb.njit("float64[:,::1](float64[:,::1], float64[:,::1])",
         fastmath=True, cache=False, error_model="numpy")
def ar_resid(Z, phi):
    n_components, n_order = phi.shape

    # compute the AR(p) residuals
    resid = Z[n_order:].copy()
    for k in range(n_order):
        # r_t -= y_{t-(p-k)} * \beta_{p - k} (phi is reversed beta)
        resid -= Z[k:k - n_order] * phi[:, k]

    return resid


# In[15]:
@nb.njit("float64[:,::1](float64[:,::1], float64[:,::1], float64[:,::1])",
         fastmath=True, cache=False, error_model="numpy")
def ar_hess_vect(V, Z, phi):
    n_components, n_order = phi.shape

    # compute the AR(p) residuals over V
    resid = ar_resid(V, phi)

    # get the derivative w.r.t. the series
    hess_v = np.zeros_like(V)
    for k in range(n_order):
        hess_v[k:k - n_order] += resid * phi[:, k]
    hess_v[n_order:] -= resid

    return hess_v


# In[16]:
@nb.njit("float64[:,::1](float64[:,::1], float64[:,::1])",
         fastmath=True, cache=False, error_model="numpy")
def ar_grad(Z, phi):
    return ar_hess_vect(Z, Z, phi)


# In[17]:
def graph_resid(F, adj):
    # `adj` is the weighted adjacency matrix: A_{ij} = \sigma_{ij} 1_{G}(i\to j)
    # get the downstream average: right mutliply by a transpose of CSR (mem CSC) is cheap
    out = safe_sparse_dot(F, adj.T, dense_output=True)

    # get the outgoing degree: |j \in G_i| = |i \to u for any u|
    deg = adj.getnnz(axis=1)[np.newaxis]

    # out_sum is zero if there are no neighbours
    mask = deg > 0
    np.divide(out, deg, where=mask, out=out)
    return np.subtract(F, out, where=mask, out=out)


# In[18]:
def graph_grad(F, adj):
    resid = graph_resid(F, adj)
    deg = np.maximum(adj.getnnz(axis=1), 1)[np.newaxis]
    return resid - safe_sparse_dot(resid / deg, adj, dense_output=True)


# In[19]:
def graph_hess_vect(V, F, adj):
    return graph_grad(V, adj)


# In[28]:
def f_step_tron_valj(f, Y, Z, C_F, eta_F, adj):
    (n_samples, n_targets), n_components = Y.shape, Z.shape[1]
    F = f.reshape(n_components, n_targets)

    objective = np.linalg.norm(Y - np.dot(Z, F), ord="fro") ** 2
    if C_F > 0:
        reg_f_l2 = np.linalg.norm(F, ord="fro") ** 2

        if sp.issparse(adj) and (eta_F > 0):
            reg_f_graph = np.linalg.norm(graph_resid(F, adj), ord="fro") ** 2
        else:
            reg_f_graph, eta_F = 0., 0.
        # end if

        reg_f = reg_f_l2 * (1 - eta_F) + reg_f_graph * eta_F
        objective += reg_f * (C_F * n_samples / n_components)
    # end if

    return 0.5 * objective


# In[30]:
def f_step_tron_grad(f, Y, Z, C_F, eta_F, adj):
    (n_samples, n_targets), n_components = Y.shape, Z.shape[1]
    coef = C_F * n_samples / n_components

    F = f.reshape(n_components, n_targets)

    ZTY, ZTZ = np.dot(Z.T, Y), np.dot(Z.T, Z)
    if (C_F > 0) and (eta_F < 1):
        ZTZ.flat[::n_components + 1] += (1 - eta_F) * coef

    grad = np.dot(ZTZ, F) - ZTY
    if (C_F > 0) and sp.issparse(adj) and (eta_F > 0):
        grad += graph_grad(F, adj) * eta_F * coef

    return grad.reshape(-1)


# In[31]:
def f_step_tron_hess(v, Y, Z, C_F, eta_F, adj):
    (n_samples, n_targets), n_components = Y.shape, Z.shape[1]
    coef = C_F * n_samples / n_components

    V = v.reshape(n_components, n_targets)

    ZTZ = np.dot(Z.T, Z)
    if (C_F > 0) and (eta_F < 1):
        ZTZ.flat[::n_components + 1] += (1 - eta_F) * coef

    hess_v = np.dot(ZTZ, V)
    if (C_F > 0) and sp.issparse(adj) and (eta_F > 0):
        hess_v += graph_grad(V, adj) * eta_F * coef

    return hess_v.reshape(-1)


# In[32]:
def f_step_tron(F, Y, Z, C_F, eta_F, adj, rtol=5e-2, atol=1e-4, verbose=False, **kwargs):
    f_call = f_step_tron_valj, f_step_tron_grad, f_step_tron_hess

    tron(f_call, F.ravel(), n_iterations=5, rtol=rtol, atol=atol,
         args=(Y, Z, C_F, eta_F, adj), verbose=verbose)

    return F


# In[33]:
def f_step_prox_func(F, Y, Z, C_F, eta_F, adj):
    return f_step_tron_valj(F.ravel(), Y, Z, C_F, eta_F, adj)


# In[34]:
def f_step_prox_grad(F, Y, Z, C_F, eta_F, adj):
    return f_step_tron_grad(F.ravel(), Y, Z, C_F, eta_F, adj).reshape(F.shape)


# In[35]:
def f_step_prox(F, Y, Z, C_F, eta_F, adj, lip=1e-2, n_iter=25, alpha=1.0, **kwargs):
    gamma_u, gamma_d = 2, 1.1

    # get the gradient
    grad = f_step_prox_grad(F, Y, Z, C_F, eta_F, adj)
    grad_F = np.dot(grad.flat, F.flat)

    f0, lip0 = f_step_prox_func(F, Y, Z, C_F, eta_F, adj), lip
    for _ in range(n_iter):
        # F_new = (1 -  alpha) * F + alpha * np.maximum(F - lr * grad, 0.)
        # prox-sgd operation
        F_new = np.maximum(F - grad / lip, 0.)

        # fgm lipschitz search
        delta = f_step_prox_func(F_new, Y, Z, C_F, eta_F, adj) - f0
        linear = np.dot(grad.flat, F_new.flat) - grad_F
        quad = np.linalg.norm(F_new - F, ord="fro") ** 2
        if delta <= linear + lip * quad / 2:
            break
        lip *= gamma_u
    # end for
#     lip = max(lip0, lip / gamma_d)
    lip = lip / gamma_d

    return F_new, lip


# In[37]:
def f_step(F, Y, Z, C_F, eta_F, adj, kind="fgm", **kwargs):
    lip = np.inf
    if kind == "fgm":
        F, lip = f_step_prox(F, Y, Z, C_F, eta_F, adj, **kwargs)
    elif kind == "tron":
        F = f_step_tron(F, Y, Z, C_F, eta_F, adj, **kwargs)
    else:
        raise ValueError(f"""Unrecognozed optiomization `{kind}`""")

    return F, lip


# In[38]:
from numpy.lib.stride_tricks import as_strided

def phi_step(phi, Z, C_Z, C_phi, eta_Z, nugget=1e-8):
    # return a set of independent AR(p) ridge estimates.
    (n_components, n_order), n_samples = phi.shape, Z.shape[0]
    if n_order < 1 or n_components < 1:
        return np.empty((n_components, n_order))

    if not ((C_Z > 0) and (eta_Z > 0)):
        return np.zeros_like(phi)

    # embed into the last dimensions
    shape = Z.shape[1:] + (Z.shape[0] - n_order, n_order + 1)
    strides = Z.strides[1:] + Z.strides[:1] + Z.strides[:1]
    Z_view = as_strided(Z, shape=shape, strides=strides)

    # split into y (d x T-p) and Z (d x T-p x p) (all are views!)
    y, Z_lagged = Z_view[..., -1], Z_view[..., :-1]

    # compute the SVD: thin, but V is d x p x p
    U, s, Vh = np.linalg.svd(Z_lagged, full_matrices=False)
    if C_phi > 0:
        # the {V^{H}}^{H} (\Sigma^2 + C I)^{-1} \Sigma part is reduced
        #  to columnwise operations
        gain = C_Z * eta_Z * n_order * s
        gain /= gain * s + C_phi * (n_samples - n_order)
    else:
        # do the same cutoff as in np.linalg.pinv(...)
        large = s > nugget * np.max(s, axis=-1, keepdims=True)
        gain = np.divide(1, s, where=large, out=s)
        gain[~large] = 0
    # end if

    # get the U' y part and the final estimate
    # $\phi_j$ corresponds to $p-j$-th lag $j = 0,\,\ldots,\,p-1$
    return np.einsum("ijk,ij,isj,is->ik", Vh, gain, U, y)


# In[43]:
def x_step_tron_valh(z, Y, F, phi, C_Z, eta_Z):
    n_samples, n_targets = Y.shape
    n_components, n_order = phi.shape
    Z = z.reshape(n_samples, n_components)

    objective = np.linalg.norm(Y - np.dot(Z, F), ord="fro") ** 2
    if C_Z > 0:
        reg_z_l2 = (np.linalg.norm(Z, ord="fro") ** 2)
        if (n_samples > n_order) and (eta_Z > 0):
            reg_z_ar_j = np.linalg.norm(ar_resid(Z, phi), ord=2, axis=0) ** 2
            reg_z_ar = np.sum(reg_z_ar_j) * n_samples / (n_samples - n_order)
        else:
            reg_z_ar, eta_Z = 0., 0.
        # end if

        reg_z = reg_z_l2 * (1 - eta_Z) + reg_z_ar * eta_Z
        objective += C_Z * n_targets / n_components * reg_z
    # end if

    return 0.5 * objective


# In[45]:
def x_step_tron_grad(z, Y, F, phi, C_Z, eta_Z):
    n_samples, n_targets = Y.shape
    n_components, n_order = phi.shape
    coef = C_Z * n_targets / n_components

    Z = z.reshape(n_samples, n_components)

    YFT, FFT = np.dot(Y, F.T), np.dot(F, F.T)
    if (C_Z > 0) and (eta_Z < 1):
        FFT.flat[::n_components + 1] += (1 - eta_Z) * coef

    grad = np.dot(Z, FFT) - YFT
    if (C_Z > 0) and (eta_Z > 0):
        ratio = n_samples / (n_samples - n_order)
        grad += ar_grad(Z, phi) * ratio * eta_Z * coef

    return grad.reshape(-1)


# In[46]:
def x_step_tron_hess(v, Y, F, phi, C_Z, eta_Z):
    n_samples, n_targets = Y.shape
    n_components, n_order = phi.shape
    coef = C_Z * n_targets / n_components

    V = v.reshape(n_samples, n_components)

    FFT = np.dot(F, F.T)
    if (C_Z > 0) and (eta_Z < 1):
        FFT.flat[::n_components + 1] += (1 - eta_Z) * coef

    hess_v = np.dot(V, FFT)
    if (C_Z > 0) and (eta_Z > 0):
        ratio = n_samples / (n_samples - n_order)
        hess_v += ar_grad(V, phi) * ratio * eta_Z * coef

    return hess_v.reshape(-1)


# In[47]:
def x_step_tron(Z, Y, F, phi, C_Z, eta_Z, rtol=5e-2, atol=1e-4, verbose=False):
    f_call = x_step_tron_valh, x_step_tron_grad, x_step_tron_hess

    tron(f_call, Z.ravel(), n_iterations=5, rtol=rtol, atol=atol,
         args=(Y, F, phi, C_Z, eta_Z), verbose=verbose)

    return Z
