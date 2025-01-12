import pyiqa
import sys
import os

sys.path.append(os.path.dirname(__file__))
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from utils.tensor_util import filepathlist2tensor
from metrics.utils.stop_watch import stop_watch


class NIQE:
    def __init__(self, device):
        self.iqa_metric = pyiqa.create_metric('niqe', device=device)

    # @stop_watch
    def calculate(self, recons, **kwargs):
        result = self.iqa_metric(recons)
        return result.cpu().item()


if __name__ == '__main__':

    device = 'cuda:0'
    
    recons_l = [
        '/mnt/d/results/20241210/074_00000034_output.png'
    ]

    gt_l = [
        '/mnt/d/results/20241210/074_00000034_gt.png'
    ]

    recons_tensor = filepathlist2tensor(recons_l)
    gt_tensor = filepathlist2tensor(gt_l)

    metric = NIQE(device)

    result = metric.calculate(recons_tensor, gt_tensor)
    print(result)
    