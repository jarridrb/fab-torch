from typing import NamedTuple, Tuple, Iterable, Callable
import torch

class ReplayData(NamedTuple):
    """Log weights and samples generated by annealed importance sampling."""
    x: torch.Tensor
    log_w: torch.Tensor
    log_q_old: torch.Tensor


class PrioritisedReplayBuffer:
    def __init__(self, dim: int,
                 max_length: int,
                 min_sample_length: int,
                 initial_sampler: Callable[[], Tuple[torch.Tensor, torch.Tensor, torch.Tensor]],
                 device: str = "cpu",
                 sample_with_replacement: bool = False
                 ):
        """
        Create prioritised replay buffer for batched sampling and adding of data.
        Args:
            dim: dimension of x data
            max_length: maximum length of the buffer
            min_sample_length: minimum length of buffer required for sampling
            initial_sampler: sampler producing x, log_w and log q, used to fill the buffer up to
                the min sample length. The initialised flow + AIS may be used here,
                or we may desire to use AIS with more distributions to give the flow a "good start".
            device: replay buffer device
            sample_with_replacement: Whether to sample from the buffer with replacement.

        The `max_length` and `min_sample_length` should be sufficiently long to prevent overfitting
        to the replay data. For example, if `min_sample_length` is equal to the
        sampling batch size, then we may overfit to the first batch of data, as we would update
        on it many times during the start of training.
        """
        assert min_sample_length < max_length
        self.dim = dim
        self.max_length = max_length
        self.min_sample_length = min_sample_length
        self.buffer = ReplayData(x=torch.zeros(self.max_length, dim).to(device),
                              log_w=torch.zeros(self.max_length, ).to(device),
                              log_q_old=torch.zeros(self.max_length, ).to(device))
        self.possible_indices = torch.arange(self.max_length).to(device)
        self.device = device
        self.current_index = 0
        self.is_full = False  # whether the buffer is full
        self.can_sample = False  # whether the buffer is full enough to begin sampling
        self.sample_with_replacement = sample_with_replacement

        while self.can_sample is False:
            # fill buffer up minimum length
            x, log_w, log_q_old = initial_sampler()
            self.add(x, log_w, log_q_old)

    @torch.no_grad()
    def add(self, x: torch.Tensor, log_w: torch.Tensor, log_q_old: torch.Tensor) -> None:
        """Add a new batch of generated data to the replay buffer"""
        batch_size = x.shape[0]
        x = x.to(self.device)
        log_w = log_w.to(self.device)
        log_q_old = log_q_old.to(self.device)
        indices = (torch.arange(batch_size) + self.current_index).to(self.device) % self.max_length
        self.buffer.x[indices] = x
        self.buffer.log_w[indices] = log_w
        self.buffer.log_q_old[indices] = log_q_old
        new_index = self.current_index + batch_size
        if not self.is_full:
            self.is_full = new_index >= self.max_length
            self.can_sample = new_index >= self.min_sample_length
        self.current_index = new_index % self.max_length

    @torch.no_grad()
    def sample(self, batch_size: int) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        """Return a batch of sampled data, if the batch size is specified then the batch will have a
        leading axis of length batch_size, otherwise the default self.batch_size will be used."""
        if not self.can_sample:
            raise Exception("Buffer must be at minimum length before calling sample")
        max_index = self.max_length if self.is_full else self.current_index
        if self.sample_with_replacement:
            indices = torch.distributions.Categorical(logits=self.buffer.log_w[:max_index]
                                                      ).sample_n(batch_size)
        else:
            sample_probs = torch.exp(
                self.buffer.log_w[:max_index] - torch.max(self.buffer.log_w[:max_index]))
            indices = torch.multinomial(sample_probs, num_samples=batch_size,
                                        replacement=False).to(self.device)
        x, log_w, log_q_old, indices = self.buffer.x[indices], self.buffer.log_w[indices], \
                                       self.buffer.log_q_old[indices], indices
        return x, log_w, log_q_old, indices


    def sample_n_batches(self, batch_size: int, n_batches: int) -> \
            Iterable[Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]]:
        """Returns a list of batches."""
        x, log_w, log_q_old, indices = self.sample(batch_size*n_batches)
        x_batches = torch.chunk(x, n_batches)
        log_w_batches = torch.chunk(log_w, n_batches)
        log_q_old_batches = torch.chunk(log_q_old, n_batches)
        indices_batches = torch.chunk(indices, n_batches)
        dataset = [(x, log_w, log_q_old, indxs) for x, log_w, log_q_old, indxs in zip(x_batches, log_w_batches,
                                                                           log_q_old_batches, indices_batches)]
        return dataset

    @torch.no_grad()
    def adjust(self, log_w_adjustment, log_q, indices):
        """Adjust log weights and log q to match new value of theta, this is typically performed
        over minibatches, rather than over the whole dataset at once."""
        valid_adjustment = ~torch.isinf(log_w_adjustment) & ~torch.isnan(log_q)
        log_w_adjustment, log_q, indices = \
            log_w_adjustment[valid_adjustment], log_q[valid_adjustment], indices[valid_adjustment]
        self.buffer.log_w[indices] += log_w_adjustment.to(self.device)
        self.buffer.log_q_old[indices] = log_q.to(self.device)

    def save(self, path):
        """Save buffer to file."""
        to_save = {'x': self.buffer.x.detach().cpu(),
                   'log_w': self.buffer.log_w.detach().cpu(),
                   'log_q_old': self.buffer.log_q_old.detach().cpu(),
                   'current_index': self.current_index,
                   'is_full': self.is_full,
                   'can_sample': self.can_sample}
        torch.save(to_save, path)

    def load(self, path):
        """Load buffer from file."""
        old_buffer = torch.load(path)
        indices = torch.arange(self.max_length)
        self.buffer.x[indices] = old_buffer['x'].to(self.device)
        self.buffer.log_w[indices] = old_buffer['log_w'].to(self.device)
        self.buffer.log_q_old[indices] = old_buffer['log_q_old'].to(self.device)
        self.current_index = old_buffer['current_index']
        self.is_full = old_buffer['is_full']
        self.can_sample = old_buffer['can_sample']




if __name__ == '__main__':
    # to check that the replay buffer runs
    dim = 5
    batch_size = 3
    n_batches_total_length = 2
    length = n_batches_total_length * batch_size
    min_sample_length = int(length * 0.5)
    initial_sampler = lambda: (torch.ones(batch_size, dim), torch.zeros(batch_size), torch.ones(batch_size))
    buffer = PrioritisedReplayBuffer(dim, length, min_sample_length, initial_sampler)
    n_batches = 3
    for i in range(100):
        buffer.add(torch.ones(batch_size, dim), torch.zeros(batch_size), torch.ones(batch_size))
        x, log_w, log_q_old, indices = buffer.sample(batch_size)
        buffer.adjust(log_w + 1, log_q_old + 0.1, indices)

