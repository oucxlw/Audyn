import torch
from dummy.modules.vqvae import Decoder, Encoder

from audyn.models.vqvae import VQVAE


def test_vqvae():
    torch.manual_seed(0)

    batch_size = 4
    codebook_size = 5
    in_channels, hidden_channels = 1, 16
    stride = 2
    num_layers = 2
    height, width = 32, 32
    latent_size = (height // (stride**num_layers), width // (stride**num_layers))

    encoder = Encoder(in_channels, hidden_channels, stride=stride, num_layers=num_layers)
    decoder = Decoder(in_channels, hidden_channels, stride=stride, num_layers=num_layers)
    model = VQVAE(encoder, decoder, codebook_size=codebook_size, embedding_dim=hidden_channels)

    input = torch.randn((batch_size, in_channels, height, width))
    reconstructed, encoded, quantized, indices = model(input)

    assert reconstructed.size() == input.size()
    assert encoded.size() == quantized.size()
    assert quantized.size(0) == indices.size(0)
    assert quantized.size(1) == hidden_channels
    assert quantized.size()[2:] == latent_size
    assert indices.size()[1:] == latent_size

    output_by_quantized = model.inference(quantized)
    output_by_indices = model.inference(indices)

    assert output_by_indices.size() == output_by_quantized.size()
