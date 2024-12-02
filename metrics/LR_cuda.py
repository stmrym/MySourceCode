import cv2
import cpbd
import numpy as np
from skimage.transform import resize
import torch
import torch.nn.functional as F
from utils_cuda import util
from utils import util as util_cpu

from utils_cuda import AnisoSetEst 
from utils_cuda.denoise_cuda import Denoise
from utils_cuda.compute_ncc import compute_ncc
from utils.compute_ncc import compute_ncc as compute_ncc_cpu

from utils.pyr_ring import align, grad_ring
from utils.stop_watch import stop_watch


class LR_Cuda:
    def __init__(self, device, **kwargs):
        self.device = device


    def _img2tensor(self, img):
        '''
        ndarray (BGR) with shape(H, W, C) -> tensor (RGB) with shape (1, C, H, W)
        '''
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        img_tensor = torch.tensor(img, device=self.device)
        img_tensor = img_tensor.permute(2, 0, 1).unsqueeze(0)
        return img_tensor


    def _tensor2img(self, img_tensor):
        '''
        tensor (RGB) with shape (1, C, H, W) -> ndarray (BGR) with shape (H, W, C)
        '''
        img = img_tensor[0].permute(1, 2, 0).cpu().numpy()
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        return img


    def _tensor_rgb2gray(self, img_tensor):
        '''
        tensor (RGB) with shape (..., C, H, W) -> tensor (Gray) with shape (..., H, W)
        '''
        weights = torch.tensor([0.299, 0.5870, 0.1140], device=img_tensor.device)
        gray_tensor = torch.tensordot(img_tensor, weights, dims=([-3], [0]))
        return gray_tensor

    # @stop_watch
    def calculate(self, img1, img2, **kwargs):
        '''
        img1: deblurred image: ndarray (BGR) [0, 255] with shape (H, W, C)
        img2: blurred image: ndarray (BGR) [0, 255] with shape (H, W, C)
        '''
        img1_tensor = self._img2tensor((img1/255).astype(np.float32))
        img2_tensor = self._img2tensor((img2/255).astype(np.float32))

        score, features = self._measure(deblurred=img1_tensor, blurred=img2_tensor)
        # print(score, features)
        return score
    

    def _measure(self, deblurred, blurred):
        '''
        deblurred: torch.Tensor (RGB) [0,1] with shape (B, C, H, W)
        blurred: torch.Tensor (RGB) [0,1] with shape (B, C, H, W)
        '''
        features = {}

        features['sparsity'] = self._sparsity(deblurred)
        features['smallgrad'] = self._smallgrad(deblurred)
        features['metric_q'] = self._metric_q(deblurred)

        # denoise = Denoise(self.device)
        # denoised = denoise.denoise(deblurred)
        denoised = deblurred

        # denoised_np = self._tensor2img(denoised)
        # cv2.imwrite('denoised.png', np.clip(denoised_np*255, 0, 255).astype(np.uint8))
        
        features['auto_corr'] = self._auto_corr(denoised)

        # print(denoised.shape)
        # features['auto_corr'] = self._auto_corr_cpu(denoised)

        features['norm_sps'] = self._norm_sparsity(denoised)

        denoised = self._tensor2img(denoised)
        blurred = self._tensor2img(blurred)
        deblurred = self._tensor2img(deblurred)


        features['cpbd'] = self._calc_cpbd(denoised)
        features['pyr_ring'] = self._pyr_ring(denoised, blurred)
        features['saturation'] = self._saturation(deblurred)
        
        score = (features['sparsity']   * -8.70515   +
                features['smallgrad']  * -62.23820  +
                features['metric_q']   * -0.04109   +
                features['auto_corr']  * -0.82738   +
                features['norm_sps']   * -13.90913  +
                features['cpbd']       * -2.20373   +
                features['pyr_ring']   * -149.19139 +
                features['saturation'] * -6.62421)

        return score, features
    

    # @stop_watch
    def _sparsity(self, img):
        '''
        img: torch.Tensor (RGB) with shape (B, C, H, W)
        '''
        dx, dy = util.gradient_cuda(img)
        d = torch.sqrt(dx**2 + dy**2)
        
        norm_l = torch.stack([util.mean_norm_cuda(d[:,c], 0.66) for c in range(d.shape[1])])
        result = torch.sum(norm_l)
        return result.cpu().item()


    # @stop_watch
    def _smallgrad(self, img):
        '''
        img: torch.Tensor (RGB) with shape (B, C, H, W)
        '''
        d = torch.zeros_like(img[:, 0, :, :])

        for c in range(img.shape[1]):
            dx, dy = util.gradient_cuda(img[:, c, :, :])
            d += torch.sqrt(dx**2 + dy**2)
        d /= 3
        
        sorted_d, _ = torch.sort(d.reshape(-1))
        n = max(int(sorted_d.numel() * 0.3), 10)
        result = util.my_sd_cuda(sorted_d[:n], 0.1)
        
        return result.cpu().item()
    

    # @stop_watch
    def _metric_q(self, img):
        '''
        img: torch.Tensor (RGB) with shape (B, C, H, W)
        '''
        PATCH_SIZE = 8
        img = self._tensor_rgb2gray(img) * 255
        result = -AnisoSetEst.MetricQ_cuda(img, PATCH_SIZE)
        return result.cpu().item()
    

    # @stop_watch
    def _auto_corr(self, img):
        '''
        img: torch.Tensor (RGB) with shape (B, C, H, W)
        '''
        img = self._tensor_rgb2gray(img)

        MARGIN = 50

        ncc_orig = compute_ncc(img, img, MARGIN)

        sizes = ncc_orig.size()
        assert sizes[0] == sizes[1]
        assert sizes[0] % 2 == 1

        # 半径を計算
        radius = sizes[0] // 2

        # 距離行列を計算
        y_dists, x_dists = torch.meshgrid(torch.arange(sizes[0], device=img.device), torch.arange(sizes[1], device=img.device))
        dists = torch.sqrt((y_dists - radius).float() ** 2 + (x_dists - radius).float() ** 2)

        # ncc の絶対値を取得
        ncc = torch.abs(ncc_orig)

        # max_m の初期化
        max_m = torch.zeros(1 + radius, device=img.device)

        # 各半径に対して計算
        for r in range(0, radius + 1):
            w = torch.abs(dists - r)
            w = torch.min(w, torch.tensor(1.0, device=img.device))
            w = 1 - w
            max_m[r] = torch.max(ncc[w > 0])

        # max_m の最初の要素を 0 に設定
        max_m[0] = 0

        # 結果を計算
        result = torch.sum(max_m)

        return result.cpu().item()



    # @stop_watch
    def _auto_corr_cpu(self, img):
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        MARGIN = 50

        ncc_orig = compute_ncc_cpu(img, img, MARGIN)


        sizes = ncc_orig.shape 
        assert sizes[0] == sizes[1] 
        assert sizes[0] % 2 == 1 
        radius = sizes[0] // 2 
        y_dists, x_dists = np.meshgrid(np.arange(sizes[0]), np.arange(sizes[1]), indexing='ij') 
        dists = np.sqrt((y_dists - radius) ** 2 + (x_dists - radius) ** 2) 
        ncc = np.abs(ncc_orig) 
        max_m = np.zeros(radius + 1) 
        for r in range(radius + 1): 
            w = np.abs(dists - r) 
            w = np.minimum(w, 1) 
            w = 1 - w 
            max_m[r] = np.max(ncc[w > 0]) 

        max_m[0] = 0 
        result = np.sum(max_m)

        return result


    # @stop_watch
    def _norm_sparsity(self, img):
        '''
        img: torch.Tensor (RGB) with shape (B, C, H, W)
        '''
        img = self._tensor_rgb2gray(img)


        dx, dy = util.gradient_cuda(img)
        d = torch.sqrt(dx**2 + dy**2)
        
        result = util.mean_norm_cuda(d, 1.0) / util.mean_norm_cuda(d, 2.0)
        return result.cpu().item()        
        


    # @stop_watch
    def _norm_sparsity_cpu(self, img):

        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        dx, dy = np.gradient(img)
        d = np.sqrt(dx**2 + dy**2)

        result = util_cpu.mean_norm(d, 1.0) / util_cpu.mean_norm(d, 2.0)
        return result

    # @stop_watch
    def _calc_cpbd(self, img):
        img = np.clip(img * 255, 0, 255).astype(np.uint8)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        return -cpbd.compute(img)

    # @stop_watch
    def _pyr_ring(self, img, blurred):

        img, blurred = align(img, blurred, True)
        height, width, color_count = img.shape

        result = 0.0
        sizes = []
        j = 0
        while True:
            coef = 0.5 ** j
            cur_height = round(height * coef)
            cur_width = round(width * coef)
            if min(cur_height, cur_width) < 16:
                break
            sizes.append([j, cur_width, cur_height])

            cur_img = resize(img, (cur_height, cur_width), order=1)
            cur_blurred = resize(blurred, (cur_height, cur_width), order=1)

            diff = grad_ring(cur_img, cur_blurred)
            if j > 0:
                result += np.mean(diff)

            j += 1

        return result

    # @stop_watch
    def _saturation(self, img):
        # 各ピクセルの最大値を計算
        max_values = np.max(img, axis=2)
        
        # 最大値が10/255以下のマスクを作成
        mask_low = (max_values <= 10.0 / 255.0)
        result_low = np.sum(mask_low.astype(np.float64)) / max_values.size

        # 各ピクセルの最小値を計算
        min_values = np.min(img, axis=2)
        
        # 最小値が1 - 10/255以上のマスクを作成
        mask_high = (min_values >= 1.0 - (10.0 / 255.0))
        result_high = np.sum(mask_high.astype(np.float64)) / min_values.size

        # 結果を計算
        result = result_low + result_high
        
        return result



if __name__ == '__main__':

    params = {'device': 'cuda:0'}

    deblurred = cv2.imread('./source_code_m/deblurred.png')
    blurred = cv2.imread('./source_code_m/blurry.png')

    metric = LR_Cuda(**params)

    result = metric.calculate(img1=deblurred, img2=blurred)

    print(f'LR_cuda: {result:.3f}')