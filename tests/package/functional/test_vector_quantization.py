import torch

from audyn.functional.vector_quantization import quantize_residual_vector


def test_quantize_residual_vector_1d() -> None:
    torch.manual_seed(0)

    batch_size = 4
    num_layers = 6
    codebook_size, embedding_dim = 10, 5
    length = 3

    input = torch.randn((batch_size, embedding_dim, length))

    # weight is tensor
    weight = torch.randn((num_layers, codebook_size, embedding_dim))
    quantized, indices = quantize_residual_vector(input, weight)

    assert quantized.size() == (batch_size, num_layers, embedding_dim, length)
    assert indices.size() == (batch_size, num_layers, length)

    # weight is list of tensors
    weight = []

    for layer_idx in range(num_layers):
        _weight = torch.randn((codebook_size + layer_idx, embedding_dim))
        weight.append(_weight)

    quantized, indices = quantize_residual_vector(input, weight)

    assert quantized.size() == (batch_size, num_layers, embedding_dim, length)
    assert indices.size() == (batch_size, num_layers, length)


def test_quantize_residual_vector_2d() -> None:
    torch.manual_seed(0)

    batch_size = 4
    num_layers = 6
    codebook_size, embedding_dim = 10, 5
    height, width = 2, 3

    input = torch.randn((batch_size, embedding_dim, height, width))

    # weight is tensor
    weight = torch.randn((num_layers, codebook_size, embedding_dim))
    quantized, indices = quantize_residual_vector(input, weight)

    assert quantized.size() == (batch_size, num_layers, embedding_dim, height, width)
    assert indices.size() == (batch_size, num_layers, height, width)

    # weight is list of tensors
    weight = []

    for layer_idx in range(num_layers):
        _weight = torch.randn((codebook_size + layer_idx, embedding_dim))
        weight.append(_weight)

    quantized, indices = quantize_residual_vector(input, weight)

    assert quantized.size() == (batch_size, num_layers, embedding_dim, height, width)
    assert indices.size() == (batch_size, num_layers, height, width)
