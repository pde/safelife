from muzero.games import safelife as sl
import numpy as np
import torch

conf = sl.MuZeroConfig()
env = sl.Game(conf)

e = sl.EmbeddingNetwork(conf)
d = sl.DynamicsNetwork(conf)
p = sl.PolicyNetwork(conf)

s1 = env.reset()
s1 = torch.tensor(s1.astype(np.float32))

embedded = e(s1[np.newaxis, ...])
print(embedded.shape)
nxt, val = d(embedded, torch.zeros(1, 9))
nxt, val2 = d(nxt, torch.zeros(1, 9))
print(nxt.shape)

act, val = p(nxt)

print(act,val)


