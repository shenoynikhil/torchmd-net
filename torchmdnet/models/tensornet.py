import torch
import numpy as np
from typing import Optional, Tuple
from torch import Tensor, nn
import torch._dynamo as dynamo
from torch_scatter import scatter
from torch_geometric.nn import MessagePassing
from torchmdnet.models.utils import (
    CosineCutoff,
    OptimizedDistance,
    rbf_class_mapping,
    act_class_mapping,
)
#torch.set_float32_matmul_precision('high')
torch.backends.cuda.matmul.allow_tf32 = True
use_graphs=True
use_ranges=False


def tensor_compile(foo):
    return torch.compile(foo, backend="inductor", disable=not use_graphs, mode="reduce-overhead")


# Creates a skew-symmetric tensor from a vector
def vector_to_skewtensor(vector):
    batch_size = vector.size(0)
    zero = torch.zeros(batch_size, device=vector.device, dtype=vector.dtype)
    tensor = torch.stack(
        (
            zero,
            -vector[:, 2],
            vector[:, 1],
            vector[:, 2],
            zero,
            -vector[:, 0],
            -vector[:, 1],
            vector[:, 0],
            zero,
        ),
        dim=1,
    )
    tensor = tensor.view(-1, 3, 3)
    return tensor.squeeze(0)


# Creates a symmetric traceless tensor from the outer product of a vector with itself
def vector_to_symtensor(vector):
    tensor = torch.matmul(vector.unsqueeze(-1), vector.unsqueeze(-2))
    I = (tensor.diagonal(offset=0, dim1=-1, dim2=-2)).mean(-1)[
        ..., None, None
    ] * torch.eye(3, 3, device=tensor.device)
    S = 0.5 * (tensor + tensor.transpose(-2, -1)) - I
    return S


# Full tensor decomposition into irreducible components
def decompose_tensor(tensor):
    I = (tensor.diagonal(offset=0, dim1=-1, dim2=-2)).mean(-1)[
        ..., None, None
    ] * torch.eye(3, 3, device=tensor.device)
    A = 0.5 * (tensor - tensor.transpose(-2, -1))
    S = 0.5 * (tensor + tensor.transpose(-2, -1)) - I
    return I, A, S


# Modifies tensor by multiplying invariant features to irreducible components
def new_radial_tensor(I, A, S, f_I, f_A, f_S):
    I = f_I[..., None, None] * I
    A = f_A[..., None, None] * A
    S = f_S[..., None, None] * S
    return I, A, S


# Computes Frobenius norm
def tensor_norm(tensor):
    return (tensor**2).sum((-2, -1))

class UncapturableTensorNet(nn.Module):
    def __init__(
        self,
        cutoff_lower=0,
        cutoff_upper=4.5,
        max_num_neighbors=64,
    ):
        super(UncapturableTensorNet, self).__init__()
        self.distance = OptimizedDistance(
            cutoff_lower, cutoff_upper, max_num_pairs=-max_num_neighbors, return_vecs=True, loop=True, check_errors=False, resize_to_fit=False
        )

    def reset_parameters(self):
        pass

    #@tensor_compile
    @dynamo.optimize(backend="cudagraphs", disable=not use_graphs)
    def forward(
        self,
        pos: Tensor,
        batch: Tensor,
    ) -> Tuple[Tensor, Tensor, Tensor]:
        # Obtain graph, with distances and relative position vectors
        edge_index, edge_weight, edge_vec = self.distance(pos, batch)
        # This assert convinces TorchScript that edge_vec is a Tensor and not an Optional[Tensor]
        assert (
            edge_vec is not None
        ), "Distance module did not return directional information"
        # mask = (edge_index[0] >= 0)
        # edge_index = edge_index[:, mask]
        # edge_weight = edge_weight[mask]
        # edge_vec = edge_vec[mask]
        # Embedding from edge-wise tensors to node-wise tensors
        #mask = (edge_index[0] != edge_index[1])
        # The line below is equivalent to the commented line, but compatible with CUDA graphs
        #edge_vec[mask] = edge_vec[mask] / edge_weight[mask].unsqueeze(1)
        return edge_index, edge_weight, edge_vec

class CapturableTensorNet(nn.Module):

    def __init__(
        self,
        hidden_channels=128,
        num_layers=2,
        num_rbf=32,
        rbf_type="expnorm",
        trainable_rbf=False,
        activation="silu",
        cutoff_lower=0,
        cutoff_upper=4.5,
        max_z=128,
        equivariance_invariance_group="O(3)",
    ):
        super(CapturableTensorNet, self).__init__()

        assert rbf_type in rbf_class_mapping, (
            f'Unknown RBF type "{rbf_type}". '
            f'Choose from {", ".join(rbf_class_mapping.keys())}.'
        )
        assert activation in act_class_mapping, (
            f'Unknown activation function "{activation}". '
            f'Choose from {", ".join(act_class_mapping.keys())}.'
        )

        assert equivariance_invariance_group in ["O(3)", "SO(3)"], (
            f'Unknown group "{equivariance_invariance_group}". '
            f"Choose O(3) or SO(3)."
        )
        self.hidden_channels = hidden_channels
        self.equivariance_invariance_group = equivariance_invariance_group
        self.num_layers = num_layers
        self.num_rbf = num_rbf
        self.rbf_type = rbf_type
        self.activation = activation
        self.cutoff_lower = cutoff_lower
        self.cutoff_upper = cutoff_upper
        act_class = act_class_mapping[activation]

        self.distance_expansion = rbf_class_mapping[rbf_type](
            cutoff_lower, cutoff_upper, num_rbf, trainable_rbf
        )
        self.tensor_embedding = TensorEmbedding(
            hidden_channels,
            num_rbf,
            act_class,
            cutoff_lower,
            cutoff_upper,
            trainable_rbf,
            max_z,
        ).jittable()

        self.layers = nn.ModuleList()
        if num_layers != 0:
            for _ in range(num_layers):
                self.layers.append(
                    Interaction(
                        num_rbf,
                        hidden_channels,
                        act_class,
                        cutoff_lower,
                        cutoff_upper,
                        equivariance_invariance_group,
                    ).jittable()
                )
        self.linear = nn.Linear(3 * hidden_channels, hidden_channels)
        self.out_norm = nn.LayerNorm(3 * hidden_channels)
        self.act = act_class()
        self.reset_parameters()

    def reset_parameters(self):
        self.tensor_embedding.reset_parameters()
        for layer in self.layers:
            layer.reset_parameters()
        self.linear.reset_parameters()
        self.out_norm.reset_parameters()
    @tensor_compile
    @dynamo.optimize(backend="cudagraphs", disable=not use_graphs)
    def forward(self, z, edge_index, edge_weight, edge_vec):
        mask = (edge_index[0] >= 0).unsqueeze(0).expand_as(edge_index)
        edge_index = edge_index*mask + (~mask)*z.size(0)
        zp = torch.cat((z, torch.zeros(1, dtype=z.dtype, device=z.device)), dim=0)
        edge_attr = self.distance_expansion(edge_weight)
        mask = (edge_index[0] != edge_index[1])
        edge_vec = edge_vec/torch.ones_like(edge_weight).masked_scatter(mask, edge_weight).unsqueeze(1)
        X = self.tensor_embedding(zp, edge_index, edge_weight, edge_vec, edge_attr)
        for layer in self.layers:
            X = layer(X, edge_index, edge_weight, edge_attr)
        I, A, S = decompose_tensor(X)
        x = torch.cat((tensor_norm(I), tensor_norm(A), tensor_norm(S)), dim=-1)
        x = self.out_norm(x)
        x = self.act(self.linear((x)))
        x = x[:-1]
        return x

class TensorNetGraph(torch.nn.Module):
    def __init__(
        self,
        hidden_channels=128,
        num_layers=2,
        num_rbf=32,
        rbf_type="expnorm",
        trainable_rbf=False,
        activation="silu",
        cutoff_lower=0,
        cutoff_upper=4.5,
        max_z=128,
        max_num_neighbors=64,
        equivariance_invariance_group="O(3)",
    ):
        super(TensorNetGraph, self).__init__()
        self.capturable = CapturableTensorNet(hidden_channels, num_layers, num_rbf, rbf_type, trainable_rbf, activation, cutoff_lower, cutoff_upper, max_z, equivariance_invariance_group)
        self.uncapturable = UncapturableTensorNet(cutoff_lower, cutoff_upper, max_num_neighbors)
        self.uncapturable =  torch.jit.script(self.uncapturable)
    def reset_parameters(self):
        self.capturable.reset_parameters()
        #self.uncapturable.reset_parameters()

    def forward(self,
                z: Tensor,
                pos: Tensor,
                batch: Tensor,
                q: Optional[Tensor] = None,
                s: Optional[Tensor] = None,
                ):
        edge_index, edge_weight, edge_vec = self.uncapturable(pos, batch)
        x = self.capturable(z, edge_index, edge_weight, edge_vec)
        return x, None, z, pos, batch

class TensorNet(nn.Module):
    r"""TensorNet's architecture, from TensorNet: Cartesian Tensor Representations
        for Efficient Learning of Molecular Potentials; G. Simeon and G. de Fabritiis.

    Args:
        hidden_channels (int, optional): Hidden embedding size.
            (default: :obj:`128`)
        num_layers (int, optional): The number of interaction layers.
            (default: :obj:`2`)
        num_rbf (int, optional): The number of radial basis functions :math:`\mu`.
            (default: :obj:`32`)
        rbf_type (string, optional): The type of radial basis function to use.
            (default: :obj:`"expnorm"`)
        trainable_rbf (bool, optional): Whether to train RBF parameters with
            backpropagation. (default: :obj:`False`)
        activation (string, optional): The type of activation function to use.
            (default: :obj:`"silu"`)
        cutoff_lower (float, optional): Lower cutoff distance for interatomic interactions.
            (default: :obj:`0.0`)
        cutoff_upper (float, optional): Upper cutoff distance for interatomic interactions.
            (default: :obj:`4.5`)
        max_z (int, optional): Maximum atomic number. Used for initializing embeddings.
            (default: :obj:`128`)
        max_num_neighbors (int, optional): Maximum number of neighbors to return for a
            given node/atom when constructing the molecular graph during forward passes.
            (default: :obj:`64`)
        equivariance_invariance_group (string, optional): Group under whose action on input
            positions internal tensor features will be equivariant and scalar predictions
            will be invariant. O(3) or SO(3).
            (default :obj:`"O(3)"`)
    """

    def __init__(
        self,
        hidden_channels=128,
        num_layers=2,
        num_rbf=32,
        rbf_type="expnorm",
        trainable_rbf=False,
        activation="silu",
        cutoff_lower=0,
        cutoff_upper=4.5,
        max_z=128,
        max_num_neighbors=64,
        equivariance_invariance_group="O(3)",
    ):
        super(TensorNet, self).__init__()

        assert rbf_type in rbf_class_mapping, (
            f'Unknown RBF type "{rbf_type}". '
            f'Choose from {", ".join(rbf_class_mapping.keys())}.'
        )
        assert activation in act_class_mapping, (
            f'Unknown activation function "{activation}". '
            f'Choose from {", ".join(act_class_mapping.keys())}.'
        )

        assert equivariance_invariance_group in ["O(3)", "SO(3)"], (
            f'Unknown group "{equivariance_invariance_group}". '
            f"Choose O(3) or SO(3)."
        )
        self.hidden_channels = hidden_channels
        self.equivariance_invariance_group = equivariance_invariance_group
        self.num_layers = num_layers
        self.num_rbf = num_rbf
        self.rbf_type = rbf_type
        self.activation = activation
        self.cutoff_lower = cutoff_lower
        self.cutoff_upper = cutoff_upper
        act_class = act_class_mapping[activation]
        self.distance = OptimizedDistance(
            cutoff_lower, cutoff_upper, max_num_pairs=-max_num_neighbors, return_vecs=True, loop=True, check_errors=False, resize_to_fit=False
        )
        # self.distance = Distance(
        #     cutoff_lower, cutoff_upper, max_num_neighbors=max_num_neighbors, return_vecs=True, loop=True
        # )

        self.distance_expansion = rbf_class_mapping[rbf_type](
            cutoff_lower, cutoff_upper, num_rbf, trainable_rbf
        )
        self.tensor_embedding = TensorEmbedding(
            hidden_channels,
            num_rbf,
            act_class,
            cutoff_lower,
            cutoff_upper,
            trainable_rbf,
            max_z,
        ).jittable()

        self.layers = nn.ModuleList()
        if num_layers != 0:
            for _ in range(num_layers):
                self.layers.append(
                    Interaction(
                        num_rbf,
                        hidden_channels,
                        act_class,
                        cutoff_lower,
                        cutoff_upper,
                        equivariance_invariance_group,
                    ).jittable()
                )
        self.linear = nn.Linear(3 * hidden_channels, hidden_channels)
        self.out_norm = nn.LayerNorm(3 * hidden_channels)
        self.act = act_class()
        self.reset_parameters()
        self.graphed = False
        num_parts = 22
        z = torch.zeros(num_parts, dtype=torch.long)
        num_pairs = max_num_neighbors * num_parts
        edge_index = torch.zeros((2,num_pairs) , dtype=torch.long)
        edge_weight = torch.zeros(num_pairs, dtype=torch.float32)
        edge_vec = torch.zeros((num_pairs, 3), dtype=torch.float32)
        self._use_interaction_layers = torch.cuda.make_graphed_callables(self._update_interaction_layers, (z, edge_index, edge_weight, edge_vec))

    def reset_parameters(self):
        self.tensor_embedding.reset_parameters()
        for layer in self.layers:
            layer.reset_parameters()
        self.linear.reset_parameters()
        self.out_norm.reset_parameters()
#    @dynamo.optimize(backend=backend, disable=not use_graphs)
    def _update_interaction_layers(self, z, edge_index, edge_weight, edge_vec):
        # Expand distances with radial basis functions
        edge_attr = self.distance_expansion(edge_weight)
        mask = (edge_index[0] != edge_index[1])
        edge_vec = edge_vec/torch.ones_like(edge_weight).masked_scatter(mask, edge_weight).unsqueeze(1)
        X = self.tensor_embedding(z, edge_index, edge_weight, edge_vec, edge_attr)
        for layer in self.layers:
            X = layer(X, edge_index, edge_weight, edge_attr)
        I, A, S = decompose_tensor(X)
        x = torch.cat((tensor_norm(I), tensor_norm(A), tensor_norm(S)), dim=-1)
        x = self.out_norm(x)
        x = self.act(self.linear((x)))
        return x
    def forward(
        self,
        z: Tensor,
        pos: Tensor,
        batch: Tensor,
        q: Optional[Tensor] = None,
        s: Optional[Tensor] = None,
    ) -> Tuple[Tensor, Optional[Tensor], Tensor, Tensor, Tensor]:
        # Obtain graph, with distances and relative position vectors
        edge_index, edge_weight, edge_vec = self.distance(pos, batch)
        # This assert convinces TorchScript that edge_vec is a Tensor and not an Optional[Tensor]
        assert (
            edge_vec is not None
        ), "Distance module did not return directional information"
        mask = edge_index[0] >= 0
        edge_index = edge_index[:, mask]
        edge_weight = edge_weight[mask]
        edge_vec = edge_vec[mask]

        # embedding from edge-wise tensors to node-wise tensors
        #mask = (edge_index[0] != edge_index[1])
        # The line below is equivalent to the commented line, but compatible with CUDA graphs
        #edge_vec[mask] = edge_vec[mask] / edge_weight[mask].unsqueeze(1)
        x = self._update_interaction_layers(z, edge_index, edge_weight, edge_vec)
        return x, None, z, pos, batch


class TensorPassing(MessagePassing):

    def __init__(self):
        super(TensorPassing, self).__init__(aggr="add", node_dim=0)


    def aggregate(
        self,
        features: Tuple[torch.Tensor, torch.Tensor, torch.Tensor],
        index: torch.Tensor,
        ptr: Optional[torch.Tensor],
        dim_size: Optional[int],
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:

        I, A, S = features
        I = scatter(I, index, dim=self.node_dim, dim_size=dim_size)
        A = scatter(A, index, dim=self.node_dim, dim_size=dim_size)
        S = scatter(S, index, dim=self.node_dim, dim_size=dim_size)
        # index = index.view(-1, 1, 1, 1)
        # I.scatter_reduce(0, index, I, reduce="sum")
        # A.scatter_reduce(0, index, A, reduce="sum")
        # S.scatter_reduce(0, index, S, reduce="sum")
        return I, A, S

    def update(
        self, inputs: Tuple[torch.Tensor, torch.Tensor, torch.Tensor]
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        return inputs


class TensorEmbedding(TensorPassing):
    def __init__(
        self,
        hidden_channels,
        num_rbf,
        activation,
        cutoff_lower,
        cutoff_upper,
        trainable_rbf=False,
        max_z=128,
    ):
        super(TensorEmbedding, self).__init__()

        self.hidden_channels = hidden_channels
        self.distance_proj1 = nn.Linear(num_rbf, hidden_channels)
        self.distance_proj2 = nn.Linear(num_rbf, hidden_channels)
        self.distance_proj3 = nn.Linear(num_rbf, hidden_channels)
        self.cutoff = CosineCutoff(cutoff_lower, cutoff_upper)
        self.max_z = max_z
        self.emb = torch.nn.Embedding(max_z, hidden_channels)
        self.emb2 = nn.Linear(2 * hidden_channels, hidden_channels)
        self.act = activation()
        self.linears_tensor = nn.ModuleList()
        for _ in range(3):
            self.linears_tensor.append(
                nn.Linear(hidden_channels, hidden_channels, bias=False)
            )
        self.linears_scalar = nn.ModuleList()
        self.linears_scalar.append(
            nn.Linear(hidden_channels, 2 * hidden_channels, bias=True)
        )
        self.linears_scalar.append(
            nn.Linear(2 * hidden_channels, 3 * hidden_channels, bias=True)
        )
        self.init_norm = nn.LayerNorm(hidden_channels)
        self.reset_parameters()

    def reset_parameters(self):
        self.distance_proj1.reset_parameters()
        self.distance_proj2.reset_parameters()
        self.distance_proj3.reset_parameters()
        self.emb.reset_parameters()
        self.emb2.reset_parameters()
        for linear in self.linears_tensor:
            linear.reset_parameters()
        for linear in self.linears_scalar:
            linear.reset_parameters()
        self.init_norm.reset_parameters()

    def forward(
        self,
        z: Tensor,
        edge_index: Tensor,
        edge_weight: Tensor,
        edge_vec_norm: Tensor,
        edge_attr: Tensor,
    ):
        Z = self.emb(z)
        C = self.cutoff(edge_weight)
        W1 = self.distance_proj1(edge_attr) * C.view(-1, 1)
        W2 = self.distance_proj2(edge_attr) * C.view(-1, 1)
        W3 = self.distance_proj3(edge_attr) * C.view(-1, 1)
        Iij, Aij, Sij = new_radial_tensor(
            torch.eye(3, 3, device=edge_vec_norm.device, dtype=edge_vec_norm.dtype)[
                None, None, :, :
            ],
            vector_to_skewtensor(edge_vec_norm)[..., None, :, :],
            vector_to_symtensor(edge_vec_norm)[..., None, :, :],
            W1,
            W2,
            W3,
        )
        # propagate_type: (Z: Tensor, I: Tensor, A: Tensor, S: Tensor)
        I, A, S = self.propagate(edge_index, Z=Z, I=Iij, A=Aij, S=Sij, size=None)
        norm = tensor_norm(I + A + S)
        norm = self.init_norm(norm)
        I = self.linears_tensor[0](I.permute(0, 2, 3, 1)).permute(0, 3, 1, 2)
        A = self.linears_tensor[1](A.permute(0, 2, 3, 1)).permute(0, 3, 1, 2)
        S = self.linears_tensor[2](S.permute(0, 2, 3, 1)).permute(0, 3, 1, 2)
        for linear_scalar in self.linears_scalar:
            norm = self.act(linear_scalar(norm))
        norm = norm.reshape(norm.shape[0], self.hidden_channels, 3)
        I, A, S = new_radial_tensor(I, A, S, norm[..., 0], norm[..., 1], norm[..., 2])
        X = I + A + S
        return X

    def message(self, Z_i, Z_j, I, A, S):
        zij = torch.cat((Z_i, Z_j), dim=-1)
        Zij = self.emb2(zij)
        I = Zij[..., None, None] * I
        A = Zij[..., None, None] * A
        S = Zij[..., None, None] * S

        return I, A, S




class Interaction(TensorPassing):
    def __init__(
        self,
        num_rbf,
        hidden_channels,
        activation,
        cutoff_lower,
        cutoff_upper,
        equivariance_invariance_group,
    ):
        super(Interaction, self).__init__()

        self.num_rbf = num_rbf
        self.hidden_channels = hidden_channels
        self.cutoff = CosineCutoff(cutoff_lower, cutoff_upper)
        self.linears_scalar = nn.ModuleList()
        self.linears_scalar.append(nn.Linear(num_rbf, hidden_channels, bias=True))
        self.linears_scalar.append(
            nn.Linear(hidden_channels, 2 * hidden_channels, bias=True)
        )
        self.linears_scalar.append(
            nn.Linear(2 * hidden_channels, 3 * hidden_channels, bias=True)
        )
        self.linears_tensor = nn.ModuleList()
        for _ in range(6):
            self.linears_tensor.append(
                nn.Linear(hidden_channels, hidden_channels, bias=False)
            )
        self.act = activation()
        self.equivariance_invariance_group = equivariance_invariance_group
        self.reset_parameters()

    def reset_parameters(self):
        for linear in self.linears_scalar:
            linear.reset_parameters()
        for linear in self.linears_tensor:
            linear.reset_parameters()

    def forward(self, X, edge_index, edge_weight, edge_attr):

        C = self.cutoff(edge_weight)
        for linear_scalar in self.linears_scalar:
            edge_attr = self.act(linear_scalar(edge_attr))
        edge_attr = (edge_attr * C.view(-1, 1)).reshape(
            edge_attr.shape[0], self.hidden_channels, 3
        )
        X = X / (tensor_norm(X) + 1)[..., None, None]
        I, A, S = decompose_tensor(X)
        I = self.linears_tensor[0](I.permute(0, 2, 3, 1)).permute(0, 3, 1, 2)
        A = self.linears_tensor[1](A.permute(0, 2, 3, 1)).permute(0, 3, 1, 2)
        S = self.linears_tensor[2](S.permute(0, 2, 3, 1)).permute(0, 3, 1, 2)
        Y = I + A + S
        # propagate_type: (I: Tensor, A: Tensor, S: Tensor, edge_attr: Tensor)
        Im, Am, Sm = self.propagate(
            edge_index, I=I, A=A, S=S, edge_attr=edge_attr, size=None
        )
        msg = Im + Am + Sm
        if self.equivariance_invariance_group == "O(3)":
            A = torch.matmul(msg, Y)
            B = torch.matmul(Y, msg)
            I, A, S = decompose_tensor(A + B)
        if self.equivariance_invariance_group == "SO(3)":
            B = torch.matmul(Y, msg)
            I, A, S = decompose_tensor(2 * B)
        normp1 = (tensor_norm(I + A + S) + 1)[..., None, None]
        I, A, S = I / normp1, A / normp1, S / normp1
        I = self.linears_tensor[3](I.permute(0, 2, 3, 1)).permute(0, 3, 1, 2)
        A = self.linears_tensor[4](A.permute(0, 2, 3, 1)).permute(0, 3, 1, 2)
        S = self.linears_tensor[5](S.permute(0, 2, 3, 1)).permute(0, 3, 1, 2)
        dX = I + A + S
        X = X + dX + dX**2
        return X

    def message(self, I_j, A_j, S_j, edge_attr):

        I, A, S = new_radial_tensor(
            I_j, A_j, S_j, edge_attr[..., 0], edge_attr[..., 1], edge_attr[..., 2]
        )
        return I, A, S
