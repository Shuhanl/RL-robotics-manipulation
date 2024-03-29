import torch
import torch.nn as nn
import torch.nn.functional as F
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
        super().__init__()
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
                                nn.Linear(layer_size, 1))

    def forward(self, vision_embed, proprioception, action):

        x = torch.cat([vision_embed, proprioception, action], dim=-1)
        q_value = F.relu(self.fc(x))

        return q_value
    

        

 

