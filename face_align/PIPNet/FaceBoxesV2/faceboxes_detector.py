from evaluation.face_align.PIPNet.FaceBoxesV2.detector import Detector
import cv2, os
import numpy as np
import torch
import torch.nn as nn
from evaluation.face_align.PIPNet.FaceBoxesV2.utils.config import cfg
from evaluation.face_align.PIPNet.FaceBoxesV2.utils.prior_box import PriorBox
# from evaluation.face_align.PIPNet.FaceBoxesV2.utils.nms_wrapper import nms
from evaluation.face_align.PIPNet.FaceBoxesV2.utils.faceboxes import FaceBoxesV2
from evaluation.face_align.PIPNet.FaceBoxesV2.utils.box_utils import decode
import time

import numpy as np

def nms(boxes, threshold):
    """
    Non-Maximum Suppression (NMS) 算法
    
    参数:
        boxes (list of lists): 每个元素为 [x1, y1, x2, y2, score]，其中 (x1, y1) 为左上角坐标，(x2, y2) 为右下角坐标，score 为置信度分数
        threshold (float): IoU 阈值，超过这个阈值的框将被抑制
    
    返回:
        list: 被保留的框的索引
    """
    if len(boxes) == 0:
        return []

    # 将列表转换为numpy数组
    boxes = np.array(boxes)
    
    # 获取坐标
    x1 = boxes[:, 0]
    y1 = boxes[:, 1]
    x2 = boxes[:, 2]
    y2 = boxes[:, 3]
    scores = boxes[:, 4]

    # 计算每个框的面积
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    
    # 根据置信度分数对框进行排序（从高到低）
    order = scores.argsort()[::-1]

    # 用于存储保留的框的索引
    keep = []

    while order.size > 0:
        # 当前得分最高的框的索引
        i = order[0]
        keep.append(i)

        # 计算当前框与其他框的相交部分的左上角和右下角坐标
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])

        # 计算相交区域的宽度和高度
        w = np.maximum(0, xx2 - xx1 + 1)
        h = np.maximum(0, yy2 - yy1 + 1)

        # 计算 IoU（交并比）
        inter = w * h
        iou = inter / (areas[i] + areas[order[1:]] - inter)

        # 保留 IoU 小于阈值的框
        inds = np.where(iou <= threshold)[0]

        # 更新排序后的索引列表
        order = order[inds + 1]

    return keep


class FaceBoxesDetector(Detector):
    def __init__(self, model_arch, model_weights, use_gpu, device):
        super().__init__(model_arch, model_weights)
        self.name = "FaceBoxesDetector"
        self.net = FaceBoxesV2(
            phase="test", size=None, num_classes=2
        )  # initialize detector
        self.use_gpu = use_gpu
        self.device = device
        state_dict = torch.load(self.model_weights, map_location=self.device)
        # create new OrderedDict that does not contain `module.`
        from collections import OrderedDict

        new_state_dict = OrderedDict()
        for k, v in state_dict.items():
            name = k[7:]  # remove `module.`
            new_state_dict[name] = v
        # load params
        self.net.load_state_dict(new_state_dict)
        self.net = self.net.to(self.device)
        self.net.eval()

    def detect(self, image, thresh=0.6, im_scale=None):
        # auto resize for large images
        if im_scale is None:
            height, width, _ = image.shape
            if min(height, width) > 600:
                im_scale = 600.0 / min(height, width)
            else:
                im_scale = 1
        image_scale = cv2.resize(
            image, None, None, fx=im_scale, fy=im_scale, interpolation=cv2.INTER_LINEAR
        )

        scale = torch.Tensor(
            [
                image_scale.shape[1],
                image_scale.shape[0],
                image_scale.shape[1],
                image_scale.shape[0],
            ]
        )
        image_scale = (
            torch.from_numpy(image_scale.transpose(2, 0, 1)).to(self.device).int()
        )
        mean_tmp = torch.IntTensor([104, 117, 123]).to(self.device)
        mean_tmp = mean_tmp.unsqueeze(1).unsqueeze(2)
        image_scale -= mean_tmp
        image_scale = image_scale.float().unsqueeze(0)
        scale = scale.to(self.device)

        with torch.no_grad():
            out = self.net(image_scale)
            # priorbox = PriorBox(cfg, out[2], (image_scale.size()[2], image_scale.size()[3]), phase='test')
            priorbox = PriorBox(
                cfg, image_size=(image_scale.size()[2], image_scale.size()[3])
            )
            priors = priorbox.forward()
            priors = priors.to(self.device)
            loc, conf = out
            prior_data = priors.data
            boxes = decode(loc.data.squeeze(0), prior_data, cfg["variance"])
            boxes = boxes * scale
            boxes = boxes.cpu().numpy()
            scores = conf.data.cpu().numpy()[:, 1]

            # ignore low scores
            inds = np.where(scores > thresh)[0]
            boxes = boxes[inds]
            scores = scores[inds]

            # keep top-K before NMS
            order = scores.argsort()[::-1][:5000]
            boxes = boxes[order]
            scores = scores[order]

            # do NMS
            dets = np.hstack((boxes, scores[:, np.newaxis])).astype(
                np.float32, copy=False
            )
            keep = nms(dets, 0.3)
            dets = dets[keep, :]

            dets = dets[:750, :]
            detections_scale = []
            for i in range(dets.shape[0]):
                xmin = int(dets[i][0])
                ymin = int(dets[i][1])
                xmax = int(dets[i][2])
                ymax = int(dets[i][3])
                score = dets[i][4]
                width = xmax - xmin
                height = ymax - ymin
                detections_scale.append(["face", score, xmin, ymin, width, height])

        # adapt bboxes to the original image size
        if len(detections_scale) > 0:
            detections_scale = [
                [
                    det[0],
                    det[1],
                    int(det[2] / im_scale),
                    int(det[3] / im_scale),
                    int(det[4] / im_scale),
                    int(det[5] / im_scale),
                ]
                for det in detections_scale
            ]

        return detections_scale, im_scale
