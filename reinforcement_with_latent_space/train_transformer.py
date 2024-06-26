import torch
import torch.optim as optim
from torch.nn.utils import clip_grad_norm_
from transformer_model import EmbeddingNetwork, PlanRecognition, PlanProposal, Actor, Critic
from noise import OrnsteinUhlenbeckProcess
from utils import compute_regularisation_loss
import parameters as params
  
class AgentTrainer():
  def __init__(self, gamma=0.95):
    self.gamma = gamma
    self.lr = params.lr
    self.device = params.device
    self.action_dim = params.action_dim
    self.batch_size = params.batch_size
    self.grad_norm_clipping = params.grad_norm_clipping
    self.goal_embeded = None
    self.sequence_length = params.sequence_length

    print(self.device)

    self.plan_recognition = PlanRecognition()
    self.plan_proposal = PlanProposal()
    self.embedding = EmbeddingNetwork()
    self.embedding_optimizer = optim.Adam(self.embedding.parameters(), lr=self.lr)
    self.plan_recognition_optimizer = optim.Adam(self.plan_recognition.parameters(), lr=self.lr)  
    self.plan_proposal_optimizer = optim.Adam(self.plan_proposal.parameters(), lr=self.lr)  

    self.actor = Actor()
    self.target_actor = Actor()
    self.target_actor.load_state_dict(self.actor.state_dict())
    self.actor_optimizer = optim.Adam(self.actor.parameters(),lr=self.lr)
    self.critic = Critic()
    self.target_critic = Critic()
    self.target_critic.load_state_dict(self.critic.state_dict())
    self.critic_optimizer = optim.Adam(self.critic.parameters(),lr=self.lr)

    self.noise = OrnsteinUhlenbeckProcess(size=self.action_dim)
    self.mse_loss = torch.nn.MSELoss()
    self.tau = params.tau
    self.beta = params.beta

    # Initialize buffers as tensors
    self.action_buffer = torch.empty((1, self.sequence_length, params.d_model)).to(self.device)
    
    # self.train_latent_buffer = torch.empty(
    #     (self.batch_size, self.sequence_length, params.latent_dim)).to(self.device)
    # self.train_action_buffer = torch.empty(
    #     (self.batch_size, self.sequence_length, params.d_model)).to(self.device)

    """ Wrap your models with DataParallel """
    if torch.cuda.device_count() > 1:
      print("Using", torch.cuda.device_count(), "GPUs!")
      self.embedding = torch.nn.DataParallel(self.embedding)
      self.plan_recognition = torch.nn.DataParallel(self.plan_recognition)

      self.actor = torch.nn.DataParallel(self.actor)
      self.critic = torch.nn.DataParallel(self.critic)
      self.target_actor = torch.nn.DataParallel(self.target_actor)
      self.target_critic = torch.nn.DataParallel(self.target_critic)

    else:
      print("Using single GPU")
      self.embedding = self.embedding.to(self.device)
      self.plan_recognition = self.plan_recognition.to(self.device)
      self.plan_proposal = self.plan_proposal.to(self.device)

      self.actor = self.actor.to(self.device)
      self.critic = self.critic.to(self.device)
      self.target_actor = self.target_actor.to(self.device)
      self.target_critic = self.target_critic.to(self.device)

  def update_buffer(self, buffer, new_data):

      return torch.cat((buffer[:, 1:, :], new_data.unsqueeze(1) ), dim=1)
    
  def set_goal(self, goal):
    goal = torch.FloatTensor(goal).to(self.device)
    goal = goal.unsqueeze(0)
    self.goal_embeded = self.embedding.vision_embed(goal)
  
  def pre_train(self, action_labels, video, proprioception):

    action_labels = action_labels.to(self.device)
    video = torch.FloatTensor(video).to(self.device)
    proprioception = torch.FloatTensor(proprioception).to(self.device)

    sequence_length = video.shape[1]
    vision_embedded = torch.stack([self.embedding.vision_embed(video[:, i, :, :, :]) for i in range(sequence_length)], dim=1)
    proprioception_embedded = self.embedding.proprioception_embed(proprioception)

    goal_embedded = vision_embedded[:, -1, :]

    action_buffer = torch.empty((self.batch_size, self.sequence_length, params.d_model)).to(self.device)

    """ Combine CNN output with proprioception data """
    recognition_dist = self.plan_recognition(vision_embedded, proprioception_embedded)
    
    """ Compute the loss for batches sequence of data """
    kl_loss, normal_kl_loss, recon_loss = 0, 0, 0
    for i in range(sequence_length):
      proposal_dist = self.plan_proposal(vision_embedded[:, i, :], proprioception_embedded[:, i, :], goal_embedded)

      kl_loss += compute_regularisation_loss(recognition_dist, proposal_dist)
      
      normal_kl_loss += torch.mean(-0.5 * torch.sum(1 + proposal_dist.scale**2 - proposal_dist.loc**2 - torch.exp(proposal_dist.scale**2), dim=1), dim=0)

      latent = proposal_dist.sample()
      """ Prepend the goal to let the network attend to it """
      
      action, _ = self.actor.get_action(vision_embedded[:, :i, :], proprioception_embedded[:, :i, :], latent, goal_embedded, action_buffer)

      action_embedded= self.embedding.action_embed(action)
    
      action_buffer = self.update_buffer(action_buffer, action_embedded)

      recon_loss += self.mse_loss(action_labels[:, i, :], action)

    # Compute the batch loss
    loss = self.beta*(kl_loss + normal_kl_loss) + recon_loss / sequence_length

    # Assuming the loss applies to all model components and they're all connected in the computational graph.
    self.embedding_optimizer.zero_grad()
    self.plan_recognition_optimizer.zero_grad()
    self.plan_proposal_optimizer.zero_grad()
    self.actor_optimizer.zero_grad()

    # Only need to call backward once if all parts are connected and contribute to the loss.
    loss.backward()

    clip_grad_norm_(self.embedding.parameters(), max_norm=self.grad_norm_clipping)
    clip_grad_norm_(self.plan_recognition.parameters(), max_norm=self.grad_norm_clipping)
    clip_grad_norm_(self.plan_proposal.parameters(), max_norm=self.grad_norm_clipping)
    clip_grad_norm_(self.actor.parameters(), max_norm=self.grad_norm_clipping)

    # Then step each optimizer
    self.embedding_optimizer.step()
    self.plan_recognition_optimizer.step()
    self.plan_proposal_optimizer.step()
    self.actor_optimizer.step()

    return loss.item()

  def get_action(self, vision, proprioception, greedy=True):

    self.embedding.eval()
    self.plan_recognition.eval()
    self.plan_proposal.eval()
    self.actor.eval()

    with torch.no_grad():
      proprioception = torch.FloatTensor(proprioception).unsqueeze(0).to(self.device)
      vision = torch.FloatTensor(vision).unsqueeze(0).to(self.device)
      vision_embeded = self.embedding.vision_embed(vision)
      vision_embeded = vision_embeded.unsqueeze(0)
      proprioception_embedded = self.embedding.proprioception_embed(proprioception)
      proprioception_embedded = proprioception_embedded.unsqueeze(0)

      proposal_dist = self.plan_proposal(vision_embeded[:, 0, :], proprioception_embedded[:, 0, :], self.goal_embeded)
      latent = proposal_dist.sample()

      action = self.actor.get_action(vision_embeded, proprioception_embedded, latent, self.goal_embeded, self.action_buffer)
      action_embedded= self.embedding.action_embed(action)
      self.action_buffer = self.update_buffer(self.action_buffer, action_embedded)

      action = action.detach().cpu().numpy()
      if not greedy:
        action += self.noise.sample()

      return action[0]
    
  def _get_next_action(self, vision_embedded, proprioception_embedded, greedy=True):
    
    with torch.no_grad():
      proposal_dist = self.plan_proposal(vision_embedded, proprioception_embedded, self.goal_embeded)
      latent = proposal_dist.sample()

      next_action = self.target_actor.get_action(vision_embedded, proprioception_embedded, latent, self.goal_embeded)
      next_action_embedded= self.embedding.action_embed(next_action)
      self.action_buffer = self.update_buffer(self.action_buffer, next_action_embedded)

      if not greedy:
          next_action += torch.tensor(self.noise.sample(),dtype=torch.float).to(self.device)
      return next_action
    
  def _td_target(self, vision_embedded, next_vision_embedded, proprioception_embedded, 
                 next_proprioception_embedded, action_embedded, reward, done):

    current_q = self.critic(vision_embedded, proprioception_embedded, action_embedded)

    next_action = self._get_next_action(vision_embedded, proprioception_embedded, greedy=True)
    next_q = self.target_critic(next_vision_embedded, next_proprioception_embedded, next_action)
    target_q = reward.unsqueeze(1) + self.gamma * next_q*(1.-done.unsqueeze(1))

    critic_loss = self.mse_loss(current_q, target_q)
    td_errors = torch.abs((target_q - current_q))          # Calculate the TD Errors

    return td_errors, critic_loss

  def init_buffer(self, vision, proprioception, action, 
              reward, next_vision, next_proprioception, done):
    
    self.pri_buffer.store(vision, proprioception, action, reward, next_vision, next_proprioception, done)


  def fine_tune(self):

    buffer = self.pri_buffer.sample_batch()
    vision, proprioception, next_vision, next_proprioception, action, reward, done, weights, indices = buffer['vision'], \
      buffer['proprioception'], buffer['next_vision'], buffer['next_proprioception'], buffer['action'], buffer['reward'], \
        buffer['done'], buffer['weights'], buffer['indices']

    vision = torch.FloatTensor(vision).to(self.device)
    proprioception = torch.FloatTensor(proprioception).to(self.device)
    next_vision = torch.FloatTensor(next_vision).to(self.device)
    next_proprioception = torch.FloatTensor(next_proprioception).to(self.device)
    action = torch.FloatTensor(action).to(self.device)
    reward = torch.FloatTensor(reward).to(self.device)
    done = torch.FloatTensor(done).to(self.device)
    goal_embedded = self.goal_embeded.repeat(vision.shape[0], 1, 1, 1)

    vision_embedded = self.embedding.vision_embed(vision)
    next_vision_embedded = self.embedding.vision_embed(next_vision)
    proprioception_embedded = self.embedding.proprioception_embed(proprioception)
    next_proprioception_embedded = self.embedding.proprioception_embed(next_proprioception)
    action_embedded = self.embedding.action_embed(action)

    td_errors, critic_loss = self._td_target(goal_embedded, vision_embedded, 
                                             next_vision_embedded, proprioception_embedded, next_proprioception_embedded, action_embedded, reward, done)

    """ Update priorities based on TD errors """
    self.pri_buffer.update_priorities(indices, td_errors.detach().cpu().numpy())
      
    """ Update critic """
    self.critic_optimizer.zero_grad()
    critic_loss.backward()
    clip_grad_norm_(self.critic.parameters(), max_norm=self.grad_norm_clipping)
    self.critic_optimizer.step()

    pr = -self.critic(vision_embedded, proprioception_embedded, action).mean()
    pg = (action.pow(2)).mean()
    actor_loss = pr + pg*1e-3

    """ Update actor """
    self.actor_optimizer.zero_grad()
    clip_grad_norm_(self.actor.parameters(), max_norm=self.grad_norm_clipping)
    actor_loss.backward()
    self.actor_optimizer.step()
  
    """ Soft update target networks """
    for target_param, param in zip(self.target_actor.parameters(), self.actor.parameters()):
      target_param.data.copy_(self.tau * param.data + (1.0 - self.tau) * target_param.data)
    for target_param, param in zip(self.target_critic.parameters(), self.critic.parameters()):
      target_param.data.copy_(self.tau * param.data + (1.0 - self.tau) * target_param.data)
  
    return critic_loss.item(), actor_loss.item()

  def save_checkpoint(self, filename):
      
    # Create a checkpoint dictionary containing the state dictionaries of all components
    checkpoint = {
        'embedding_state_dict': self.embedding.state_dict(),
        'plan_recognition_state_dict': self.plan_recognition.state_dict(),
        'plan_proposal_state_dict': self.plan_proposal.state_dict(),
        'actor_state_dict': self.actor.state_dict(),
        'target_actor_state_dict': self.target_actor.state_dict(),
        'critic_state_dict': self.critic.state_dict(),
        'target_critic_state_dict': self.target_critic.state_dict(),
    }
    
    # Use torch.save to serialize and save the checkpoint dictionary
    torch.save(checkpoint, filename)
    print('Model saved')

  def load_checkpoint(self, filename):
      checkpoint = torch.load(filename)

      self.embedding.load_state_dict(checkpoint['embedding_state_dict'])
      self.plan_recognition.load_state_dict(checkpoint['plan_recognition_state_dict'])
      self.plan_proposal.load_state_dict(checkpoint['plan_proposal_state_dict'])

      self.actor.load_state_dict(checkpoint['actor_state_dict'])
      self.target_actor.load_state_dict(checkpoint['target_actor_state_dict'])

      self.critic.load_state_dict(checkpoint['critic_state_dict'])
      self.target_critic.load_state_dict(checkpoint['target_critic_state_dict'])

      print('Model loaded')