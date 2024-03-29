import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions import Normal
from torch.distributions import Categorical, Distribution, AffineTransform, TransformedDistribution, SigmoidTransform
from torch.distributions.mixture_same_family import MixtureSameFamily
from torch.distributions.uniform import Uniform
import parameters as params
    
class VisionNetwork(nn.Module):
    def __init__(self, ):
        super(VisionNetwork, self).__init__()

        img_channels=params.vision_dim[0]
        img_height=params.vision_dim[1]
        img_width=params.vision_dim[2]
        self.device = params.device
        self.out_dim = params.vision_embedding_dim

        # Encoder Layers
        self.cnn = nn.Sequential(
        nn.Conv2d(img_channels, 32, kernel_size=8, stride=4, padding=0),  
        nn.ReLU(),
        nn.Conv2d(32, 64, kernel_size=4, stride=2, padding=0),
        nn.ReLU(),
        nn.Conv2d(64, 64, kernel_size=3, stride=1, padding=0),
        nn.ReLU()
        )

        # to easily figure out the dimensions after flattening, we pass a test tensor
        test_tensor = torch.zeros(
            [img_channels, img_height, img_width]
        )
        with torch.no_grad():
            pre_softmax = self.cnn(test_tensor[None])
            N, C, H, W = pre_softmax.shape
        
            self.fc = nn.Sequential(nn.Linear(2*C, 512),
                                nn.ReLU(),
                                nn.Linear(512, self.out_dim))
    
    def _spatial_softmax(self, pre_softmax):
        N, C, H, W = pre_softmax.shape
        pre_softmax = pre_softmax.view(N*C, H * W)
        softmax = F.softmax(pre_softmax, dim=1)
        softmax = softmax.view(N, C, H, W)

        # Create normalized meshgrid
        x_coords = torch.linspace(0, 1, W, device=self.device)
        y_coords = torch.linspace(0, 1, H, device=self.device)
        X, Y = torch.meshgrid(x_coords, y_coords, indexing='xy')
        image_coords = torch.stack([X, Y], dim=-1).to(self.device)  # [H, W, 2]

        image_coords = image_coords.unsqueeze(0)  # [1, H, W, 2]
        image_coords = image_coords.unsqueeze(0)  # [1, H, W, 2] -> [1, 1, H, W, 2]
        
        # Reshape softmax for broadcasting
        softmax = softmax.unsqueeze(-1)  # [N, C, H, W, 1]

        # Compute spatial soft argmax
        # This tensor represents the 'center of mass' for each channel of each feature map in the batch
        spatial_soft_argmax = torch.sum(softmax * image_coords, dim=[2, 3])  # [N, C, 2]
        x = nn.Flatten()(spatial_soft_argmax)  # [N, C, 2] -> [N, 2*C]
        return x

    def forward(self, x):
        pre_softmax = self.cnn(x)
        spatial_soft_argmax = self._spatial_softmax(pre_softmax)
        x = self.fc(spatial_soft_argmax)
        return x

        
class PlanRecognition(nn.Module):
    def __init__(self):
        super(PlanRecognition, self).__init__()
        self.layer_size=2048
        self.epsilon=1e-4
        self.in_dim = params.vision_embedding_dim + params.proprioception_dim
        self.latent_dim = params.latent_dim

        # Encoder Layers
        self.lstm1 = nn.LSTM(self.in_dim, self.layer_size, batch_first=True, bidirectional=True)
        self.lstm2 = nn.LSTM(2 * self.layer_size, self.layer_size, batch_first=True, bidirectional=True)
        self.mu = nn.Linear(2 * self.layer_size, self.latent_dim)
        self.sigma = nn.Linear(2 * self.layer_size, self.latent_dim)

    def latent_normal(self, mu, sigma):
        dist = Normal(loc=mu, scale=sigma)
        return dist

    def forward(self, x):

        # LSTM Layers
        x, _ = self.lstm1(x)
        x, _ = self.lstm2(x)
        # Latent variable
        x = x[:, -1, :]  # Take the last element of the sequence
        mu = self.mu(x)
        sigma = F.softplus(self.sigma(x+self.epsilon))
        dist = self.latent_normal(mu, sigma)

        return dist

class PlanRecognitionTransformer(nn.Module):
    def __init__(self):
        super(PlanRecognitionTransformer, self).__init__()
        self.layer_size=2048
        self.nhead=params.n_heads
        self.epsilon=1e-4
        self.max_seq_length = 1000
        self.in_dim = params.vision_embedding_dim + params.proprioception_dim
        self.latent_dim = params.latent_dim

        self.position_embedding = nn.Embedding(self.max_seq_length, self.in_dim)

        # Define the Transformer encoder layer (self-attention on the same sensor embedding sequence)
        encoder_layers = nn.TransformerEncoderLayer(d_model=self.in_dim, nhead=self.nhead, dim_feedforward=self.layer_size, batch_first=True)
        self.transformer_encoder = nn.TransformerEncoder(encoder_layers, num_layers=1)
    
        # Transformer Decoder (Cross attention on different sensors embeddings sequences)
        decoder_layer = nn.TransformerDecoderLayer(d_model=self.in_dim, nhead=self.nhead, dim_feedforward=self.layer_size, batch_first=True)
        self.transformer_decoder = nn.TransformerDecoder(decoder_layer, num_layers=1)


        # Linear layers for the latent variables
        self.mu = nn.Linear(self.in_dim, self.latent_dim)
        self.sigma = nn.Linear(self.in_dim, self.latent_dim)

    def latent_normal(self, mu, sigma):
        dist = Normal(loc=mu, scale=sigma)
        return dist

    def forward(self, x):

        # Determine sequence length from the current input
        seq_length = x.size(1)  # Assuming [batch, seq_length, features]
        
        # Generate positional indices based on the current sequence length
        positions = torch.arange(seq_length, device=x.device).expand((x.size(0), seq_length)).contiguous()
        
        # Add position embeddings
        pos_embeddings = self.position_embedding(positions)
        x += pos_embeddings

        # Encoder
        memory = self.transformer_encoder(x)

        # Decoder with Cross-Attention
        output = self.transformer_decoder(x, memory)

        # Process output for latent variables as before
        output = output[:, -1, :]
        mu = self.mu(output)
        sigma = F.softplus(self.sigma(output) + self.epsilon)
        dist = self.latent_normal(mu, sigma)

        return dist
    
class PlanProposal(nn.Module):
    def __init__(self):
        super(PlanProposal, self).__init__()
        self.layer_size=2048
        self.epsilon=1e-4

        # Encoder Layers
        self.in_features = 2*params.vision_embedding_dim + params.proprioception_dim
        self.latent_dim = params.latent_dim

        self.fc = nn.Sequential(nn.Linear(self.in_features, self.layer_size), 
                                        nn.ReLU(),
                                        nn.Linear(self.layer_size, self.layer_size),
                                        nn.ReLU(),
                                        nn.Linear(self.layer_size, self.layer_size),
                                        nn.ReLU(),
                                        nn.Linear(self.layer_size, self.layer_size),
                                        nn.ReLU(),
                                        nn.Linear(self.layer_size, self.latent_dim))

    def latent_normal(self, mu, sigma):
        dist = Normal(loc=mu, scale=sigma)
        return dist

    def forward(self, vision_embeded, proprioception, goal_embeded):
        """
        x: (bs, input_size) -> input_size: goal (vision only) + current (visuo-proprio)
        """
        x = torch.cat([vision_embeded, proprioception, goal_embeded], dim=-1)
        mu = self.fc(x)
        sigma = F.softplus(self.fc(x+self.epsilon))
        dist = self.latent_normal(mu, sigma)

        return dist


class LogisticMixture(Distribution):
    def __init__(self, weightings, mu, scale, qbits):
        """
        Initializes the logistic mixture model.
        :param weightings: The logits for the categorical distribution.
        :param mu: The means of the logistic distributions.
        :param scale: The scales of the logistic distributions.
        :param qbits: Number of quantization bits.
        """
        super(LogisticMixture, self).__init__()
        arg_constraints = {}
        
        # Create a uniform distribution as the base for the logistic transformation
        base_distributions = Uniform(torch.zeros_like(mu), torch.ones_like(mu))
        
        # Apply the logistic transformation to the uniform base
        # Logistic(x; mu, scale) = mu + scale * log(x / (1-x))
        # Define transforms: Inverse of sigmoid followed by an affine transformation
        transforms = [SigmoidTransform().inv, AffineTransform(loc=mu, scale=scale)]

        # Create the transformed distribution representing the logistic distribution
        logistic_distributions = TransformedDistribution(base_distributions, transforms)
        
        # Create the mixture distribution
        self.mixture_dist = MixtureSameFamily(
            mixture_distribution=Categorical(logits=weightings),
            component_distribution=logistic_distributions,
        )
               
    def sample(self, sample_shape=torch.Size()):
        """
        Samples from the logistic mixture model.
        """
        x = self.mixture_dist.sample(sample_shape)
        x = torch.clamp(x, -1, 1)
        return x

    def log_prob(self, value):
        log_prob = self.mixture_dist.log_prob(value)
        return log_prob
    
class LogisticActor(nn.Module):
  def __init__(self, layer_size=1024, epsilon=1e-4):
    super(LogisticActor, self).__init__()
    self.epsilon = epsilon
    self.in_dim = 2*params.vision_embedding_dim + params.latent_dim + params.proprioception_dim
    self.action_dim = params.action_dim
    self.num_distribs = params.num_distribs
    self.device = params.device
    self.qbits = params.qbits

    self.lstm1 = nn.LSTM(input_size=self.in_dim,
                          hidden_size=layer_size, batch_first=True)
    self.lstm2 = nn.LSTM(input_size=layer_size, hidden_size=layer_size, batch_first=True)

    self.alpha = nn.Linear(layer_size, self.action_dim * self.num_distribs)
    self.mu = nn.Linear(layer_size, self.action_dim * self.num_distribs)
    self.sigma = nn.Linear(layer_size, self.action_dim * self.num_distribs)

  def forward(self, vision_embeded, proprioception, latent, goal_embeded):

    x = torch.cat([vision_embeded, proprioception, latent, goal_embeded], dim=-1)

    x, _ = self.lstm1(x)
    x, _ = self.lstm2(x)

    weightings = self.alpha(x).view(-1, self.action_dim, self.num_distribs)
    mu = self.mu(x).view(-1, self.action_dim, self.num_distribs)
    scale = torch.nn.functional.softplus(self.sigma(x + self.epsilon)).view(-1, self.action_dim, self.num_distribs)
    logistic_mixture = LogisticMixture(weightings, mu, scale, self.qbits)

    return logistic_mixture
    
class DirectActor(nn.Module):
    def __init__(
        self,
        layer_size=1024
    ):
        super(DirectActor, self).__init__()
        self.action_dim = params.action_dim

        self.in_dim = 2*params.vision_embedding_dim + params.latent_dim + params.proprioception_dim
        self.lstm1 = nn.LSTM(input_size=self.in_dim, hidden_size=layer_size, batch_first=True)                    
        self.lstm2 = nn.LSTM(input_size=layer_size, hidden_size=layer_size, batch_first=True)

        self.fc = nn.Sequential(nn.Linear(layer_size, self.action_dim), nn.Tanh())

    def forward(self, vision_embeded, proprioception, latent, goal_embeded):
        x = torch.cat([vision_embeded, proprioception, latent, goal_embeded], dim=-1)
        x, _ = self.lstm1(x)
        x, _ = self.lstm2(x)
        action = self.fc(x)
        return action

class DirectActorTransformer(nn.Module):
    def __init__(
        self,
        layer_size=1024, 
    ):
        super(DirectActorTransformer, self).__init__()
        self.max_seq_length = 10
        self.nhead = params.n_heads
        self.action_dim = params.action_dim
        self.in_dim = 2*params.vision_embedding_dim + params.latent_dim + params.proprioception_dim
        self.device = params.device
        self.batch_size = params.batch_size

        # Initialize history buffers as tensors
        self.vision_embeded_buffer = torch.zeros((self.batch_size, self.max_seq_length, params.vision_embedding_dim)).to(self.device)
        self.proprioception_buffer = torch.zeros((self.batch_size, self.max_seq_length, params.proprioception_dim)).to(self.device)
        self.latent_buffer = torch.zeros((self.batch_size, self.max_seq_length, params.latent_dim)).to(self.device)
        self.goal_embeded_buffer = torch.zeros((self.batch_size, self.max_seq_length, params.vision_embedding_dim)).to(self.device)

        self.position_embedding = nn.Embedding(self.max_seq_length, self.in_dim)

        # Transformer Encoder Layer for self-attention on the same embedding sequence
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=self.in_dim, nhead=self.nhead, dim_feedforward=layer_size, batch_first=True
        )
        self.transformer_encoder = nn.TransformerEncoder(encoder_layer, num_layers=1)

        # Transformer Decoder Layer for cross-attention on different embedding sequences
        decoder_layer = nn.TransformerDecoderLayer(
            d_model=self.in_dim, nhead=self.nhead, dim_feedforward=layer_size, batch_first=True
        )
        self.transformer_decoder = nn.TransformerDecoder(decoder_layer, num_layers=1)

        # Output fully connected layer
        self.fc = nn.Sequential(nn.Linear(self.in_dim, self.action_dim), nn.Tanh())

    def update_buffer(self, buffer, new_data):

        return torch.cat((buffer[:, 1:, :], new_data.unsqueeze(1) ), dim=1)
    
    def forward(self, vision_embeded, proprioception, latent, goal_embeded):

        # Update history buffers
        self.vision_embeded_buffer = self.update_buffer(self.vision_embeded_buffer, vision_embeded)
        self.proprioception_buffer = self.update_buffer(self.proprioception_buffer, proprioception)
        self.latent_buffer = self.update_buffer(self.latent_buffer, latent)
        self.goal_embeded_buffer = self.update_buffer(self.goal_embeded_buffer, goal_embeded)

        # Concatenate historical data from all buffers
        x = torch.cat([
            self.vision_embeded_buffer, 
            self.proprioception_buffer, 
            self.latent_buffer, 
            self.goal_embeded_buffer
        ], dim=-1)
        
        # Generate positional indices and add position embeddings
        positions = torch.arange(self.max_seq_length, device=self.device).expand((self.batch_size, self.max_seq_length)).contiguous()
        pos_embeddings = self.position_embedding(positions)
        x += pos_embeddings

        memory = self.transformer_encoder(x)
        output = self.transformer_decoder(x, memory)
        
        # Use the last decoder output for generating the action
        output = output[:, -1, :]
        
        action = self.fc(output)
        
        return action

class Critic(nn.Module):
    def __init__(self, layer_size=1024):
        super(Critic, self).__init__()
        self.in_features = params.vision_embedding_dim + params.proprioception_dim + params.action_dim
        # Fully Connected Layers
        self.fc = nn.Sequential(nn.Linear(self.in_features, layer_size), 
                                nn.ReLU(),
                                nn.Linear(layer_size, layer_size),
                                nn.ReLU(),
                                nn.Linear(layer_size, layer_size),
                                nn.ReLU(),
                                nn.Linear(layer_size, 1), 
                                nn.ReLU())

    def forward(self, vision_embed, proprioception, action):

        x = torch.cat([vision_embed, proprioception, action], dim=-1)
        q_value = self.fc(x)

        return q_value
    

        

 

