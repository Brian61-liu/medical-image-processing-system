"""
医学图像处理综合应用系统 / Medical Image Processing Workbench

基于 PyQt5 + OpenCV 的桌面端医学影像处理与分析工具。

主要功能：
- 图像读取：单张图像、图像文件夹浏览、DICOM (.dcm) 读取与窗宽窗位调节
- 增强与滤波：卡尔曼滤波（双向）、伪彩色增强
- 边缘检测：Roberts、Sobel、Canny，参数滑块实时刷新
- 纹理分析：LBP 局部二值模式及直方图、GLCM 灰度共生矩阵特征
- 分割：Otsu 阈值 + 形态学处理 + 轮廓叠加
- 组合流程：卡尔曼滤波 + Sobel / Roberts / LBP
- ROI：框选感兴趣区域，仅处理该区域并做定量分析（灰度、清晰度、信息熵、边缘比例、GLCM）
- 评价与报告：处理前后灰度直方图对比、图像质量评价、一键生成分析报告
- 批量处理：多算法批量输出并导出 CSV 指标表
- 语音控制（可选）：科大讯飞语音听写 WebSocket API，中文口令免鼠标操作

安装依赖：
pip install -r requirements.txt

语音控制（可选）需设置环境变量：
setx XFYUN_APPID "你的APPID"
setx XFYUN_API_KEY "你的APIKey"
setx XFYUN_API_SECRET "你的APISecret"

运行：
python medical_image_system.py
"""

import os
import sys
import time
import threading
import re
import difflib
import base64
import hashlib
import hmac
import json
import ssl
import csv
from datetime import datetime
from email.utils import formatdate
from time import mktime
from typing import List, Optional, Tuple
from collections import deque
from urllib.parse import urlencode

import cv2
import numpy as np

try:
    import pydicom
    PYDICOM_AVAILABLE = True
except Exception:
    pydicom = None
    PYDICOM_AVAILABLE = False

# 科大讯飞云语音识别为可选功能：没有安装相关库时，图像处理程序仍然可以正常运行。
try:
    import websocket
    import sounddevice as sd
    XFYUN_VOICE_AVAILABLE = True
except Exception:
    websocket = None
    sd = None
    XFYUN_VOICE_AVAILABLE = False

from PyQt5.QtCore import Qt, QTimer, QSize, pyqtSignal
from PyQt5.QtGui import QFont, QImage, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
    QDialog,
    QTextEdit,
)


# =========================================================
# 一、文件读取与保存工具函数
# =========================================================

SUPPORTED_IMAGE_EXTENSIONS = [".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".dcm"]


def read_image_unicode(file_path: str) -> Optional[np.ndarray]:
    """
    读取图像，兼容 Windows 中文路径。
    OpenCV 的 cv2.imread 在中文路径下有时会失败，因此使用 np.fromfile + cv2.imdecode。
    """
    try:
        image_data = np.fromfile(file_path, dtype=np.uint8)
        image = cv2.imdecode(image_data, cv2.IMREAD_COLOR)
        return image
    except Exception:
        return None


def save_image_unicode(file_path: str, image: np.ndarray) -> bool:
    """
    保存图像，兼容 Windows 中文路径。
    """
    try:
        ext = os.path.splitext(file_path)[1].lower()
        if ext == "":
            ext = ".png"
            file_path += ext

        success, encoded_img = cv2.imencode(ext, image)
        if not success:
            return False
        encoded_img.tofile(file_path)
        return True
    except Exception:
        return False


def dicom_pixels_to_uint8(pixel_array: np.ndarray, window_center: Optional[float] = None, window_width: Optional[float] = None) -> np.ndarray:
    """
    将 DICOM 原始像素转换为 8 bit 灰度图。

    技术特点：
    1. 若提供窗宽窗位，则按医学影像显示方式进行窗变换；
    2. 若未提供窗宽窗位，则使用百分位拉伸，避免极端值影响显示；
    3. 输出 uint8，便于后续 OpenCV 与 PyQt 显示。
    """
    arr = pixel_array.astype(np.float32)

    if window_center is not None and window_width is not None and window_width > 1:
        low = float(window_center) - float(window_width) / 2.0
        high = float(window_center) + float(window_width) / 2.0
    else:
        low = float(np.percentile(arr, 1.0))
        high = float(np.percentile(arr, 99.0))

    if high <= low:
        return normalize_to_uint8(arr)

    out = (arr - low) * 255.0 / (high - low)
    return np.clip(out, 0, 255).astype(np.uint8)


def _as_float_value(value, default: Optional[float] = None) -> Optional[float]:
    """
    兼容 DICOM 中 WindowCenter / WindowWidth 可能为 MultiValue 的情况。
    """
    try:
        if isinstance(value, (list, tuple)):
            return float(value[0])
        if hasattr(value, "__iter__") and not isinstance(value, (str, bytes)):
            return float(list(value)[0])
        return float(value)
    except Exception:
        return default


def read_medical_image(file_path: str) -> Tuple[Optional[np.ndarray], dict]:
    """
    统一读取普通图像和 DICOM 医学影像。

    返回：
    - image_bgr：用于界面显示与处理的 BGR 图像
    - metadata：包含是否 DICOM、窗宽窗位、原始像素、患者信息屏蔽后的基础影像信息

    注意：本系统只读取必要的技术元数据，不显示患者隐私信息。
    """
    ext = os.path.splitext(file_path)[1].lower()
    metadata = {"is_dicom": False}

    if ext == ".dcm":
        if not PYDICOM_AVAILABLE:
            return None, {"is_dicom": True, "error": "未安装 pydicom，请执行 pip install pydicom"}
        try:
            ds = pydicom.dcmread(file_path)
            raw = ds.pixel_array.astype(np.float32)

            slope = _as_float_value(getattr(ds, "RescaleSlope", 1.0), 1.0) or 1.0
            intercept = _as_float_value(getattr(ds, "RescaleIntercept", 0.0), 0.0) or 0.0
            raw = raw * slope + intercept

            wc = _as_float_value(getattr(ds, "WindowCenter", None), None)
            ww = _as_float_value(getattr(ds, "WindowWidth", None), None)
            if wc is None:
                wc = float(np.percentile(raw, 50.0))
            if ww is None or ww <= 1:
                ww = float(np.percentile(raw, 99.0) - np.percentile(raw, 1.0))

            gray = dicom_pixels_to_uint8(raw, wc, ww)
            image_bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
            metadata = {
                "is_dicom": True,
                "raw_pixels": raw,
                "window_center": float(wc),
                "window_width": float(ww),
                "default_window_center": float(wc),
                "default_window_width": float(ww),
                "min_value": float(np.min(raw)),
                "max_value": float(np.max(raw)),
                "modality": str(getattr(ds, "Modality", "未知")),
                "rows": int(getattr(ds, "Rows", raw.shape[0])),
                "columns": int(getattr(ds, "Columns", raw.shape[1])),
                "pixel_spacing": str(getattr(ds, "PixelSpacing", "未知")),
            }
            return image_bgr, metadata
        except Exception as e:
            return None, {"is_dicom": True, "error": str(e)}

    image = read_image_unicode(file_path)
    return image, metadata


def get_image_files_from_folder(folder_path: str) -> List[str]:
    """
    从文件夹中读取所有支持格式的图像文件路径。
    """
    image_files = []
    for name in os.listdir(folder_path):
        ext = os.path.splitext(name)[1].lower()
        if ext in SUPPORTED_IMAGE_EXTENSIONS:
            image_files.append(os.path.join(folder_path, name))
    image_files.sort()
    return image_files


# =========================================================
# 二、图像处理基础函数
# =========================================================


def convert_to_gray(image: np.ndarray) -> np.ndarray:
    """
    转换为灰度图。
    """
    if len(image.shape) == 2:
        return image.copy()
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def normalize_to_uint8(image: np.ndarray) -> np.ndarray:
    """
    将任意数值范围的图像归一化为 0~255 的 uint8 图像。

    优化点：
    1. 使用绝对值处理梯度图，避免负梯度丢失；
    2. 遇到全黑、全白或常数图时直接返回零图，防止除零错误；
    3. 统一输出 uint8，保证 PyQt 显示和 OpenCV 保存稳定。
    """
    if image is None:
        return None
    image = np.abs(image).astype(np.float32)
    if np.max(image) == np.min(image):
        return np.zeros_like(image, dtype=np.uint8)
    result = cv2.normalize(image, None, 0, 255, cv2.NORM_MINMAX)
    return result.astype(np.uint8)


def percentile_stretch(image: np.ndarray, low_percent: float = 1.0, high_percent: float = 99.0) -> np.ndarray:
    """
    医学图像常存在极亮骨骼、极暗背景等极端灰度值。
    直接 min-max 归一化容易被极端值影响。
    这里使用百分位拉伸增强主要组织结构的对比度。
    """
    if image is None:
        return None

    gray = image.astype(np.float32)
    low = np.percentile(gray, low_percent)
    high = np.percentile(gray, high_percent)

    if high <= low:
        return normalize_to_uint8(gray)

    stretched = (gray - low) * 255.0 / (high - low)
    stretched = np.clip(stretched, 0, 255)
    return stretched.astype(np.uint8)


def medical_contrast_enhance(gray_image: np.ndarray, clip_limit: float = 2.0, tile_grid_size: Tuple[int, int] = (8, 8)) -> np.ndarray:
    """
    医学灰度图像对比度增强。

    使用 CLAHE（限制对比度自适应直方图均衡化）增强局部细节，
    比普通直方图均衡化更不容易把噪声放大过度。
    """
    if gray_image is None:
        return None

    gray = percentile_stretch(gray_image, 1.0, 99.0)
    clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=tile_grid_size)
    enhanced = clahe.apply(gray)
    return enhanced


def adaptive_edge_threshold(edge_image: np.ndarray, parameter_value: int, low_percentile: float, high_percentile: float) -> np.ndarray:
    """
    自适应边缘阈值。

    旧版本使用固定阈值，面对 X 光、CT、MRI、超声等不同灰度范围时不够稳定。
    新版本根据当前图像边缘强度的百分位自动取阈值：
    - 参数小：保留更多弱边缘；
    - 参数大：只保留更强、更清晰的主边缘。
    """
    if edge_image is None:
        return None

    value = int(np.clip(parameter_value, 1, 100))
    edge = edge_image.astype(np.uint8)
    nonzero = edge[edge > 0]
    if nonzero.size == 0:
        return edge

    percentile = low_percentile + (value - 1) / 99.0 * (high_percentile - low_percentile)
    threshold_value = np.percentile(nonzero, percentile)
    _, result = cv2.threshold(edge, threshold_value, 255, cv2.THRESH_TOZERO)
    return result.astype(np.uint8)


def apply_threshold(edge_image: np.ndarray, threshold_value: int) -> np.ndarray:
    """
    保留旧接口，供部分功能调用。
    对边缘图像进行固定阈值处理。
    """
    threshold_value = int(np.clip(threshold_value, 0, 255))
    if threshold_value <= 0:
        return edge_image
    _, binary = cv2.threshold(edge_image, threshold_value, 255, cv2.THRESH_TOZERO)
    return binary


def draw_roi_rectangle(image: np.ndarray, roi: Optional[Tuple[int, int, int, int]]) -> np.ndarray:
    """
    在原图上绘制 ROI 矩形框，方便界面显示当前选择区域。
    """
    if image is None:
        return image

    output = image.copy()
    if roi is None:
        return output

    x, y, w, h = roi
    if w <= 0 or h <= 0:
        return output

    # 使用浅灰色边框，适合医学灰度图显示
    cv2.rectangle(output, (x, y), (x + w, y + h), (220, 220, 220), 3)
    return output


# =========================================================
# 三、四种图像处理算法（优化版）
# =========================================================


def kalman_filter_1d(signal: np.ndarray, process_noise: float, measurement_noise: float) -> np.ndarray:
    """
    一维卡尔曼滤波。

    特点：
    - 递推估计像素灰度值；
    - 降低随机噪声；
    - 保持灰度变化趋势。
    """
    if len(signal) == 0:
        return signal

    signal_float = signal.astype(np.float32) / 255.0

    x_est = signal_float[0]
    p_est = 1.0
    filtered_signal = np.zeros_like(signal_float)

    for i in range(len(signal_float)):
        # 预测阶段
        x_pred = x_est
        p_pred = p_est + process_noise

        # 更新阶段
        kalman_gain = p_pred / (p_pred + measurement_noise)
        x_est = x_pred + kalman_gain * (signal_float[i] - x_pred)
        p_est = (1 - kalman_gain) * p_pred

        filtered_signal[i] = x_est

    filtered_signal = np.clip(filtered_signal * 255, 0, 255).astype(np.uint8)
    return filtered_signal


def kalman_filter_1d_forward_backward(signal: np.ndarray, process_noise: float, measurement_noise: float) -> np.ndarray:
    """
    前向 + 反向卡尔曼滤波。

    旧版本只沿一个方向递推，图像可能出现轻微方向性平滑。
    新版本正向滤波一次、反向滤波一次，再取平均，减少方向偏差。
    """
    forward = kalman_filter_1d(signal, process_noise, measurement_noise)
    backward = kalman_filter_1d(signal[::-1], process_noise, measurement_noise)[::-1]
    result = ((forward.astype(np.float32) + backward.astype(np.float32)) / 2.0).astype(np.uint8)
    return result


def kalman_filter_image(gray_image: np.ndarray, parameter_value: int = 50) -> np.ndarray:
    """
    二维图像卡尔曼滤波优化版。

    parameter_value：1~100。
    数值越大，平滑去噪越明显。

    优化点：
    1. 使用前向 + 反向滤波，减少方向性偏差；
    2. 分别进行行方向和列方向滤波，兼顾水平和垂直噪声；
    3. 与原图按比例融合，避免医学结构边缘被过度抹平；
    4. 最后轻微双边滤波，减少随机噪声，同时尽量保留边缘。
    """
    if gray_image is None:
        return None

    value = int(np.clip(parameter_value, 1, 100))
    gray = percentile_stretch(gray_image, 0.5, 99.5)

    # 参数设计：滑块越大，越相信模型估计，平滑越强。
    process_noise = 1e-5 + (101 - value) / 100.0 * 2e-5
    measurement_noise = 0.004 + (value / 100.0) * 0.10

    row_filtered = np.zeros_like(gray)
    for i in range(gray.shape[0]):
        row_filtered[i, :] = kalman_filter_1d_forward_backward(gray[i, :], process_noise, measurement_noise)

    col_filtered = np.zeros_like(row_filtered)
    for j in range(row_filtered.shape[1]):
        col_filtered[:, j] = kalman_filter_1d_forward_backward(row_filtered[:, j], process_noise, measurement_noise)

    # 融合原图和滤波结果：参数越大，滤波结果占比越高。
    alpha = 0.45 + value / 100.0 * 0.45
    fused = cv2.addWeighted(gray, 1.0 - alpha, col_filtered, alpha, 0)

    # 轻微边缘保持平滑，用于让卡尔曼结果视觉上更稳定。
    denoised = cv2.bilateralFilter(fused, d=5, sigmaColor=20 + value * 0.4, sigmaSpace=5)
    return denoised


def roberts_operator(gray_image: np.ndarray, parameter_value: int = 30) -> np.ndarray:
    """
    Roberts算子边缘检测优化版。

    Roberts特点：
    - 2×2交叉模板；
    - 对斜向、细小、局部灰度突变边缘敏感；
    - 速度快，但容易受噪声影响。

    优化点：
    1. 先进行轻度中值滤波，减少孤立噪点；
    2. 用CLAHE增强局部组织对比度；
    3. 使用百分位自适应阈值，不同类型医学图像更稳定；
    4. 保持边缘细线条，突出Roberts“细边缘、快速检测”的特点。
    """
    if gray_image is None:
        return None

    enhanced = medical_contrast_enhance(gray_image, clip_limit=1.8, tile_grid_size=(8, 8))
    smoothed = cv2.medianBlur(enhanced, 3)
    gray = smoothed.astype(np.float32)

    kernel_x = np.array([[1, 0], [0, -1]], dtype=np.float32)
    kernel_y = np.array([[0, 1], [-1, 0]], dtype=np.float32)

    grad_x = cv2.filter2D(gray, cv2.CV_32F, kernel_x)
    grad_y = cv2.filter2D(gray, cv2.CV_32F, kernel_y)

    edge = cv2.magnitude(grad_x, grad_y)
    edge = percentile_stretch(edge, 2.0, 98.5)
    edge = adaptive_edge_threshold(edge, parameter_value, low_percentile=62.0, high_percentile=94.0)

    # Roberts强调细边缘，不做明显膨胀，只进行轻微去孤立点。
    kernel = np.ones((2, 2), np.uint8)
    edge = cv2.morphologyEx(edge, cv2.MORPH_OPEN, kernel, iterations=1)
    return edge


def sobel_gradient(gray_image: np.ndarray, parameter_value: int = 30) -> np.ndarray:
    """
    Sobel梯度边缘检测优化版。

    Sobel特点：
    - 3×3梯度模板；
    - 同时计算水平和垂直方向梯度；
    - 边缘连续性通常比Roberts更好；
    - 适合提取主要组织轮廓和结构边界。

    优化点：
    1. 使用CLAHE增强医学图像局部结构；
    2. 高斯平滑减少噪声导致的虚假边缘；
    3. 使用百分位自适应阈值；
    4. 轻微形态学闭运算，让主边缘更连续，突出Sobel稳定性。
    """
    if gray_image is None:
        return None

    enhanced = medical_contrast_enhance(gray_image, clip_limit=2.0, tile_grid_size=(8, 8))
    blurred = cv2.GaussianBlur(enhanced, (3, 3), 0.8)
    gray = blurred.astype(np.float32)

    grad_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    grad_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    gradient = cv2.magnitude(grad_x, grad_y)

    gradient = percentile_stretch(gradient, 1.0, 99.0)
    gradient = adaptive_edge_threshold(gradient, parameter_value, low_percentile=58.0, high_percentile=92.0)

    # Sobel强调主要结构轮廓，轻微闭运算增强连续性。
    kernel = np.ones((3, 3), np.uint8)
    gradient = cv2.morphologyEx(gradient, cv2.MORPH_CLOSE, kernel, iterations=1)
    return gradient


def lbp_local_binary_pattern(gray_image: np.ndarray, parameter_value: int = 50) -> np.ndarray:
    """
    LBP局部二值模式优化版。

    LBP特点：
    - 描述中心像素与周围像素的局部灰度关系；
    - 更关注纹理模式，而不是单纯边缘；
    - 适合展示医学图像中组织纹理、粗糙度和局部结构差异。

    优化点：
    1. 先做轻度中值滤波，减少噪声点对二值编码的干扰；
    2. 保留8邻域标准LBP编码，便于解释原理；
    3. 引入局部标准差图作为纹理强度提示；
    4. 通过参数滑块控制LBP纹理图的显示增强程度。
    """
    if gray_image is None:
        return None

    value = int(np.clip(parameter_value, 1, 100))
    gray = medical_contrast_enhance(gray_image, clip_limit=1.6, tile_grid_size=(8, 8))
    gray = cv2.medianBlur(gray, 3)
    h, w = gray.shape

    if h < 3 or w < 3:
        return gray.copy()

    lbp = np.zeros((h - 2, w - 2), dtype=np.uint8)
    center = gray[1:h - 1, 1:w - 1]

    # 按顺时针方向比较8邻域像素与中心像素。
    lbp |= ((gray[0:h - 2, 0:w - 2] >= center).astype(np.uint8) << 7)
    lbp |= ((gray[0:h - 2, 1:w - 1] >= center).astype(np.uint8) << 6)
    lbp |= ((gray[0:h - 2, 2:w] >= center).astype(np.uint8) << 5)
    lbp |= ((gray[1:h - 1, 2:w] >= center).astype(np.uint8) << 4)
    lbp |= ((gray[2:h, 2:w] >= center).astype(np.uint8) << 3)
    lbp |= ((gray[2:h, 1:w - 1] >= center).astype(np.uint8) << 2)
    lbp |= ((gray[2:h, 0:w - 2] >= center).astype(np.uint8) << 1)
    lbp |= ((gray[1:h - 1, 0:w - 2] >= center).astype(np.uint8) << 0)

    lbp_result = cv2.copyMakeBorder(lbp, 1, 1, 1, 1, cv2.BORDER_REPLICATE)

    # 局部标准差用于突出纹理强度。
    gray_float = gray.astype(np.float32)
    local_mean = cv2.blur(gray_float, (7, 7))
    local_sq_mean = cv2.blur(gray_float * gray_float, (7, 7))
    local_std = np.sqrt(np.maximum(local_sq_mean - local_mean * local_mean, 0))
    local_std = percentile_stretch(local_std, 1.0, 99.0)

    equalized_lbp = cv2.equalizeHist(lbp_result)
    alpha = value / 100.0

    # 参数越大，纹理编码和局部纹理强度越突出。
    lbp_enhanced = cv2.addWeighted(lbp_result, 1.0 - alpha, equalized_lbp, alpha, 0)
    result = cv2.addWeighted(lbp_enhanced, 0.78, local_std, 0.22, 0)
    return result


# =========================================================
# 四、扩展技术性图像处理与定量分析功能
# =========================================================


def canny_edge_detection(gray_image: np.ndarray, parameter_value: int = 50) -> np.ndarray:
    """
    Canny边缘检测。

    技术特点：
    1. 高斯平滑抑制噪声；
    2. 梯度方向非极大值抑制，使边缘更细；
    3. 双阈值连接强弱边缘，边缘连续性通常优于单纯梯度阈值法。

    parameter_value 控制阈值严格程度：数值越大，只保留更强边缘。
    """
    if gray_image is None:
        return None

    value = int(np.clip(parameter_value, 1, 100))
    enhanced = medical_contrast_enhance(gray_image, clip_limit=2.0, tile_grid_size=(8, 8))
    blurred = cv2.GaussianBlur(enhanced, (5, 5), 1.0)

    # 采用中位数自适应阈值，再由滑块控制阈值范围。
    med = float(np.median(blurred))
    scale = 0.45 + value / 100.0 * 0.85
    lower = int(max(0, med * (1.0 - scale)))
    upper = int(min(255, med * (1.0 + scale)))
    if upper <= lower:
        upper = min(255, lower + 30)

    edges = cv2.Canny(blurred, lower, upper, L2gradient=True)
    return edges


def otsu_morphology_contour(gray_image: np.ndarray, parameter_value: int = 50) -> np.ndarray:
    """
    Otsu自动阈值分割 + 形态学处理 + 轮廓叠加。

    技术特点：
    1. Otsu自动阈值无需人工指定分割阈值；
    2. 形态学开运算去除小噪点；
    3. 形态学闭运算填补区域小空洞；
    4. 将分割轮廓叠加到原灰度图上，便于观察器官或组织边界。

    parameter_value 控制形态学核大小和迭代强度。
    """
    if gray_image is None:
        return None

    value = int(np.clip(parameter_value, 1, 100))
    enhanced = medical_contrast_enhance(gray_image, clip_limit=2.0, tile_grid_size=(8, 8))
    blurred = cv2.GaussianBlur(enhanced, (5, 5), 0.8)

    _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # 自动判断前景方向：若白色区域过大，反转，避免背景被当作前景。
    white_ratio = np.mean(binary > 0)
    if white_ratio > 0.60:
        binary = 255 - binary

    kernel_size = 3 if value < 45 else 5 if value < 80 else 7
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
    opened = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel, iterations=1)
    closed = cv2.morphologyEx(opened, cv2.MORPH_CLOSE, kernel, iterations=1 + (value // 60))

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    overlay = cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)

    min_area = max(20, int(gray_image.shape[0] * gray_image.shape[1] * 0.0004))
    valid_contours = [c for c in contours if cv2.contourArea(c) >= min_area]

    # 绿色填充掩膜、红色轮廓，突出“分割区域 + 边界定位”的特点。
    mask_color = np.zeros_like(overlay)
    cv2.drawContours(mask_color, valid_contours, -1, (0, 180, 0), thickness=-1)
    overlay = cv2.addWeighted(overlay, 0.78, mask_color, 0.22, 0)
    cv2.drawContours(overlay, valid_contours, -1, (0, 0, 255), thickness=2)
    return overlay


def pseudo_color_enhancement(gray_image: np.ndarray, parameter_value: int = 50) -> np.ndarray:
    """
    医学灰度图像伪彩色增强。

    技术特点：
    1. 将单通道灰度映射为彩色，增强灰度层次差异；
    2. 低参数使用 BONE 风格，适合X光/CT；
    3. 中参数使用 JET 风格，灰度层次最明显；
    4. 高参数使用 HOT 风格，突出高灰度区域。
    """
    if gray_image is None:
        return None

    value = int(np.clip(parameter_value, 1, 100))
    enhanced = medical_contrast_enhance(gray_image, clip_limit=1.8, tile_grid_size=(8, 8))
    if value < 34:
        cmap = cv2.COLORMAP_BONE
    elif value < 67:
        cmap = cv2.COLORMAP_JET
    else:
        cmap = cv2.COLORMAP_HOT
    return cv2.applyColorMap(enhanced, cmap)


def calculate_entropy(gray_image: np.ndarray) -> float:
    """计算灰度信息熵，表示图像灰度信息丰富程度。"""
    if gray_image is None:
        return 0.0
    gray = gray_image.astype(np.uint8)
    hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).flatten()
    prob = hist / max(float(np.sum(hist)), 1.0)
    prob = prob[prob > 0]
    return float(-np.sum(prob * np.log2(prob)))


def calculate_sharpness(gray_image: np.ndarray) -> float:
    """用 Laplacian 方差衡量清晰度，值越大通常表示边缘和细节越明显。"""
    if gray_image is None:
        return 0.0
    return float(cv2.Laplacian(gray_image.astype(np.uint8), cv2.CV_64F).var())


def calculate_edge_ratio(gray_image: np.ndarray) -> float:
    """计算边缘像素比例，用于边缘检测结果或原图结构复杂度评价。"""
    if gray_image is None:
        return 0.0
    edges = cv2.Canny(gray_image.astype(np.uint8), 60, 160, L2gradient=True)
    return float(np.mean(edges > 0))


def calculate_image_quality_metrics(image: np.ndarray) -> dict:
    """
    计算图像质量与结构统计指标。

    指标含义：
    - 平均灰度：整体亮度水平；
    - 标准差/对比度：灰度波动与组织差异；
    - 信息熵：灰度信息丰富程度；
    - 清晰度：边缘和细节强度；
    - 边缘比例：结构边界占比；
    - 纹理复杂度：局部灰度变化强弱。
    """
    if image is None:
        return {}
    gray = convert_to_gray(image) if len(image.shape) == 3 else image.copy()
    gray = gray.astype(np.uint8)
    std = float(np.std(gray))
    metrics = {
        "平均灰度": float(np.mean(gray)),
        "灰度标准差": std,
        "对比度": std,
        "信息熵": calculate_entropy(gray),
        "清晰度": calculate_sharpness(gray),
        "边缘像素比例": calculate_edge_ratio(gray),
    }

    # 纹理复杂度：局部标准差均值归一化。
    gf = gray.astype(np.float32)
    local_mean = cv2.blur(gf, (7, 7))
    local_sq_mean = cv2.blur(gf * gf, (7, 7))
    local_std = np.sqrt(np.maximum(local_sq_mean - local_mean * local_mean, 0))
    metrics["纹理复杂度"] = float(np.mean(local_std) / 255.0)
    return metrics


def calculate_glcm_features(gray_image: np.ndarray, levels: int = 32, distance: int = 1) -> dict:
    """
    计算 GLCM 灰度共生矩阵纹理特征。避免额外依赖 skimage，使用 NumPy 手写实现。

    技术特点：
    - LBP 偏局部二值编码；
    - GLCM 偏灰度空间关系统计；
    - 二者结合可以从不同角度描述医学图像纹理。
    """
    if gray_image is None:
        return {}

    gray = gray_image.astype(np.uint8)
    if gray.size == 0:
        return {}

    # 降低灰度级，保证计算速度，同时保留纹理统计趋势。
    quantized = (gray.astype(np.float32) / 256.0 * levels).astype(np.int32)
    quantized = np.clip(quantized, 0, levels - 1)

    offsets = [(0, distance), (distance, 0), (distance, distance), (-distance, distance)]
    glcm = np.zeros((levels, levels), dtype=np.float64)
    h, w = quantized.shape

    for dy, dx in offsets:
        y_start = max(0, -dy)
        y_end = min(h, h - dy)
        x_start = max(0, -dx)
        x_end = min(w, w - dx)
        a = quantized[y_start:y_end, x_start:x_end].ravel()
        b = quantized[y_start + dy:y_end + dy, x_start + dx:x_end + dx].ravel()
        if a.size == 0:
            continue
        np.add.at(glcm, (a, b), 1)
        np.add.at(glcm, (b, a), 1)  # 对称化

    total = np.sum(glcm)
    if total <= 0:
        return {}
    p = glcm / total

    i, j = np.indices((levels, levels))
    contrast = float(np.sum((i - j) ** 2 * p))
    homogeneity = float(np.sum(p / (1.0 + np.abs(i - j))))
    energy = float(np.sum(p ** 2))
    entropy = float(-np.sum(p[p > 0] * np.log2(p[p > 0])))

    mu_i = float(np.sum(i * p))
    mu_j = float(np.sum(j * p))
    sigma_i = float(np.sqrt(np.sum(((i - mu_i) ** 2) * p)))
    sigma_j = float(np.sqrt(np.sum(((j - mu_j) ** 2) * p)))
    if sigma_i * sigma_j == 0:
        correlation = 0.0
    else:
        correlation = float(np.sum((i - mu_i) * (j - mu_j) * p) / (sigma_i * sigma_j))

    return {
        "对比度 Contrast": contrast,
        "同质性 Homogeneity": homogeneity,
        "能量 Energy": energy,
        "相关性 Correlation": correlation,
        "熵 Entropy": entropy,
        "灰度级 Levels": float(levels),
    }


# =========================================================
# 四、ROI处理逻辑
# =========================================================


def process_with_optional_roi(
    gray_image: np.ndarray,
    roi: Optional[Tuple[int, int, int, int]],
    algorithm_name: str,
    parameter_value: int,
) -> np.ndarray:
    """
    如果没有ROI，则处理整张灰度图；
    如果有ROI，则只处理ROI区域，并把结果嵌回整张图中。
    """
    if gray_image is None:
        return None

    def run_algorithm(input_gray: np.ndarray) -> np.ndarray:
        if algorithm_name == "卡尔曼滤波":
            return kalman_filter_image(input_gray, parameter_value)
        if algorithm_name == "Roberts算子":
            return roberts_operator(input_gray, parameter_value)
        if algorithm_name == "Sobel梯度":
            return sobel_gradient(input_gray, parameter_value)
        if algorithm_name == "LBP局部二值模式":
            return lbp_local_binary_pattern(input_gray, parameter_value)
        if algorithm_name == "Canny边缘检测":
            return canny_edge_detection(input_gray, parameter_value)
        if algorithm_name == "Otsu分割+形态学轮廓":
            return otsu_morphology_contour(input_gray, parameter_value)
        if algorithm_name == "伪彩色增强":
            return pseudo_color_enhancement(input_gray, parameter_value)

        # 组合处理流程：先使用卡尔曼滤波去噪，再进行边缘或纹理分析。
        # 这种流程更接近实际医学图像处理中“预处理 + 特征提取”的步骤。
        if algorithm_name == "卡尔曼滤波 + Sobel梯度":
            denoised = kalman_filter_image(input_gray, parameter_value)
            return sobel_gradient(denoised, parameter_value)
        if algorithm_name == "卡尔曼滤波 + Roberts算子":
            denoised = kalman_filter_image(input_gray, parameter_value)
            return roberts_operator(denoised, parameter_value)
        if algorithm_name == "卡尔曼滤波 + LBP局部二值模式":
            denoised = kalman_filter_image(input_gray, parameter_value)
            return lbp_local_binary_pattern(denoised, parameter_value)

        return input_gray.copy()

    if roi is None:
        return run_algorithm(gray_image)

    x, y, w, h = roi
    if w <= 0 or h <= 0:
        return run_algorithm(gray_image)

    height, width = gray_image.shape
    x = max(0, min(x, width - 1))
    y = max(0, min(y, height - 1))
    w = max(1, min(w, width - x))
    h = max(1, min(h, height - y))

    roi_gray = gray_image[y:y + h, x:x + w]
    roi_processed = run_algorithm(roi_gray)

    # 以灰度原图作为背景，只替换ROI区域。
    # 如果算法输出彩色图（例如伪彩色或轮廓叠加），则自动转为BGR背景再嵌入。
    if roi_processed is not None and len(roi_processed.shape) == 3:
        result = cv2.cvtColor(gray_image, cv2.COLOR_GRAY2BGR)
        result[y:y + h, x:x + w] = roi_processed
        cv2.rectangle(result, (x, y), (x + w, y + h), (0, 255, 255), 2)
    else:
        result = gray_image.copy()
        result[y:y + h, x:x + w] = roi_processed
        cv2.rectangle(result, (x, y), (x + w, y + h), 255, 2)
    return result


# =========================================================
# 五、OpenCV图像转PyQt显示
# =========================================================


def cv_image_to_qpixmap(image: np.ndarray) -> QPixmap:
    """
    OpenCV 图像转 QPixmap。
    支持灰度图和彩色图。
    """
    if image is None:
        return QPixmap()

    if len(image.shape) == 2:
        height, width = image.shape
        bytes_per_line = width
        q_image = QImage(image.data, width, height, bytes_per_line, QImage.Format_Grayscale8)
        return QPixmap.fromImage(q_image.copy())

    rgb_image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    height, width, channel = rgb_image.shape
    bytes_per_line = channel * width
    q_image = QImage(rgb_image.data, width, height, bytes_per_line, QImage.Format_RGB888)
    return QPixmap.fromImage(q_image.copy())


class StableImageLabel(QLabel):
    """
    稳定的图像显示控件。

    作用：
    1. 固定图像显示框大小。
    2. 防止 QLabel 在 setPixmap 后根据图片尺寸改变 sizeHint。
    3. 防止点击算法按钮或拖动滑块时，图像边框逐渐变大、界面整体下移。
    """

    def __init__(self, text: str = "", width: int = 580, height: int = 430):
        super().__init__(text)
        self.display_width = width
        self.display_height = height

        self.setAlignment(Qt.AlignCenter)
        self.setFixedSize(width, height)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setScaledContents(False)
        self.setWordWrap(True)
        self.setStyleSheet("background-color: #111111; color: white; border: 1px solid #333333;")

    def sizeHint(self) -> QSize:
        return QSize(self.display_width, self.display_height)

    def minimumSizeHint(self) -> QSize:
        return QSize(self.display_width, self.display_height)


def set_label_image(label: QLabel, image: np.ndarray) -> None:
    """
    将图像显示到 QLabel，并按固定显示框等比例缩放。
    """
    if image is None:
        return

    pixmap = cv_image_to_qpixmap(image)
    scaled_pixmap = pixmap.scaled(
        label.width(),
        label.height(),
        Qt.KeepAspectRatio,
        Qt.SmoothTransformation,
    )
    label.setText("")
    label.setPixmap(scaled_pixmap)


# =========================================================
# 六、放大查看窗口
# =========================================================


class ImagePreviewDialog(QDialog):
    """
    放大查看原图像和处理后的图像。
    """

    def __init__(self, original_image: np.ndarray, processed_image: np.ndarray, algorithm_name: str):
        super().__init__()
        self.original_image = original_image
        self.processed_image = processed_image
        self.algorithm_name = algorithm_name

        self.setWindowTitle("图像放大查看")
        self.resize(1200, 720)
        self.init_ui()

    def init_ui(self) -> None:
        main_layout = QHBoxLayout()

        self.original_label = QLabel()
        self.processed_label = QLabel()

        for label in [self.original_label, self.processed_label]:
            label.setAlignment(Qt.AlignCenter)
            label.setStyleSheet("background-color: black; border: 1px solid gray;")
            label.setMinimumSize(540, 560)

        left_layout = QVBoxLayout()
        left_title = QLabel("原图像")
        left_title.setAlignment(Qt.AlignCenter)
        left_title.setFont(QFont("Microsoft YaHei", 14, QFont.Bold))
        left_layout.addWidget(left_title)
        left_layout.addWidget(self.original_label)

        right_layout = QVBoxLayout()
        right_title = QLabel(f"处理后的图像：{self.algorithm_name}")
        right_title.setAlignment(Qt.AlignCenter)
        right_title.setFont(QFont("Microsoft YaHei", 14, QFont.Bold))
        right_layout.addWidget(right_title)
        right_layout.addWidget(self.processed_label)

        main_layout.addLayout(left_layout)
        main_layout.addLayout(right_layout)
        self.setLayout(main_layout)
        self.show_images()

    def show_images(self) -> None:
        set_label_image(self.original_label, self.original_image)
        set_label_image(self.processed_label, self.processed_image)

    def resizeEvent(self, event) -> None:
        self.show_images()
        super().resizeEvent(event)


# =========================================================
# 七、一键对比窗口
# =========================================================


class CompareDialog(QDialog):
    """
    一键对比窗口：原图、卡尔曼滤波、Roberts、Sobel、LBP。
    """

    def __init__(self, images_dict: dict):
        super().__init__()
        self.images_dict = images_dict
        self.setWindowTitle("一键对比结果")
        self.resize(1250, 850)
        self.init_ui()

    def init_ui(self) -> None:
        outer_layout = QVBoxLayout()
        title = QLabel("医学图像处理结果一键对比")
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QFont("Microsoft YaHei", 18, QFont.Bold))
        outer_layout.addWidget(title)

        grid = QGridLayout()
        positions = [
            ("原图像", 0, 0),
            ("卡尔曼滤波", 0, 1),
            ("Roberts算子", 0, 2),
            ("Sobel梯度", 1, 0),
            ("LBP局部二值模式", 1, 1),
        ]

        for name, row, col in positions:
            block = QVBoxLayout()
            label_title = QLabel(name)
            label_title.setAlignment(Qt.AlignCenter)
            label_title.setFont(QFont("Microsoft YaHei", 12, QFont.Bold))
            image_label = StableImageLabel("", 360, 260)
            set_label_image(image_label, self.images_dict.get(name))
            block.addWidget(label_title)
            block.addWidget(image_label)
            grid.addLayout(block, row, col)

        outer_layout.addLayout(grid)
        self.setLayout(outer_layout)


# =========================================================
# 八、LBP直方图窗口
# =========================================================


class LbpHistogramDialog(QDialog):
    """
    显示LBP直方图和纹理统计信息。
    """

    def __init__(self, lbp_image: np.ndarray):
        super().__init__()
        self.lbp_image = lbp_image
        self.setWindowTitle("LBP纹理直方图与统计信息")
        self.resize(900, 650)
        self.init_ui()

    def init_ui(self) -> None:
        layout = QVBoxLayout()

        title = QLabel("LBP局部二值模式纹理直方图")
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QFont("Microsoft YaHei", 16, QFont.Bold))
        layout.addWidget(title)

        hist_image = self.create_histogram_image(self.lbp_image)
        hist_label = QLabel()
        hist_label.setAlignment(Qt.AlignCenter)
        hist_label.setMinimumSize(820, 360)
        set_label_image(hist_label, hist_image)
        layout.addWidget(hist_label)

        info_text = QTextEdit()
        info_text.setReadOnly(True)
        info_text.setFont(QFont("Microsoft YaHei", 11))
        info_text.setText(self.create_statistics_text(self.lbp_image))
        layout.addWidget(info_text)

        self.setLayout(layout)

    @staticmethod
    def create_histogram_image(lbp_image: np.ndarray) -> np.ndarray:
        """
        使用OpenCV绘制直方图图像，避免额外依赖matplotlib。
        """
        gray = lbp_image.astype(np.uint8)
        hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).flatten()
        hist = hist / max(hist.max(), 1)

        width, height = 820, 360
        canvas = np.ones((height, width, 3), dtype=np.uint8) * 255

        margin_left = 50
        margin_right = 20
        margin_top = 30
        margin_bottom = 45
        plot_w = width - margin_left - margin_right
        plot_h = height - margin_top - margin_bottom

        # 坐标轴
        cv2.line(canvas, (margin_left, margin_top), (margin_left, margin_top + plot_h), (0, 0, 0), 2)
        cv2.line(canvas, (margin_left, margin_top + plot_h), (margin_left + plot_w, margin_top + plot_h), (0, 0, 0), 2)

        bin_w = plot_w / 256
        for i in range(256):
            x1 = int(margin_left + i * bin_w)
            x2 = int(margin_left + (i + 1) * bin_w)
            bar_h = int(hist[i] * plot_h)
            y1 = margin_top + plot_h - bar_h
            y2 = margin_top + plot_h
            cv2.rectangle(canvas, (x1, y1), (max(x2, x1 + 1), y2), (80, 80, 80), -1)

        cv2.putText(canvas, "0", (margin_left - 5, height - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 1)
        cv2.putText(canvas, "255", (margin_left + plot_w - 35, height - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 1)
        cv2.putText(canvas, "LBP Value", (width // 2 - 55, height - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 1)
        cv2.putText(canvas, "Frequency", (8, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 1)

        return canvas

    @staticmethod
    def create_statistics_text(lbp_image: np.ndarray) -> str:
        """
        生成LBP统计信息。
        """
        img = lbp_image.astype(np.float32)
        mean_value = float(np.mean(img))
        std_value = float(np.std(img))
        max_value = int(np.max(img))
        min_value = int(np.min(img))

        # 简单纹理复杂度：标准差越高，纹理变化越丰富
        complexity = std_value / 255.0

        return (
            "LBP纹理统计信息：\n\n"
            f"1. LBP均值：{mean_value:.2f}\n"
            f"2. LBP标准差：{std_value:.2f}\n"
            f"3. LBP最大值：{max_value}\n"
            f"4. LBP最小值：{min_value}\n"
            f"5. 纹理复杂度：{complexity:.4f}\n\n"
            "说明：\n"
            "LBP均值可以反映图像局部纹理编码的整体水平；\n"
            "LBP标准差可以反映局部纹理变化的丰富程度；\n"
            "纹理复杂度越高，说明图像局部灰度变化越明显，纹理结构越丰富。"
        )



# =========================================================
# 九、灰度直方图对比窗口与实验分析报告窗口
# =========================================================


class GrayHistogramCompareDialog(QDialog):
    """
    显示原图像与处理后图像的灰度直方图对比。
    该功能用于从数据角度说明算法处理前后灰度分布的变化。
    """

    def __init__(self, original_gray: np.ndarray, processed_image: np.ndarray, algorithm_name: str):
        super().__init__()
        self.original_gray = original_gray
        self.processed_gray = convert_to_gray(processed_image) if len(processed_image.shape) == 3 else processed_image
        self.algorithm_name = algorithm_name
        self.setWindowTitle("处理前后灰度直方图对比")
        self.resize(1100, 720)
        self.init_ui()

    def init_ui(self) -> None:
        layout = QVBoxLayout()

        title = QLabel(f"处理前后灰度直方图对比：{self.algorithm_name}")
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QFont("Microsoft YaHei", 16, QFont.Bold))
        layout.addWidget(title)

        image_layout = QHBoxLayout()

        original_hist = self.create_gray_histogram_image(self.original_gray, "Original")
        processed_hist = self.create_gray_histogram_image(self.processed_gray, "Processed")

        left_layout = QVBoxLayout()
        left_title = QLabel("原图像灰度直方图")
        left_title.setAlignment(Qt.AlignCenter)
        left_title.setFont(QFont("Microsoft YaHei", 12, QFont.Bold))
        left_label = QLabel()
        left_label.setAlignment(Qt.AlignCenter)
        set_label_image(left_label, original_hist)
        left_layout.addWidget(left_title)
        left_layout.addWidget(left_label)

        right_layout = QVBoxLayout()
        right_title = QLabel("处理后图像灰度直方图")
        right_title.setAlignment(Qt.AlignCenter)
        right_title.setFont(QFont("Microsoft YaHei", 12, QFont.Bold))
        right_label = QLabel()
        right_label.setAlignment(Qt.AlignCenter)
        set_label_image(right_label, processed_hist)
        right_layout.addWidget(right_title)
        right_layout.addWidget(right_label)

        image_layout.addLayout(left_layout)
        image_layout.addLayout(right_layout)
        layout.addLayout(image_layout)

        info = QTextEdit()
        info.setReadOnly(True)
        info.setFont(QFont("Microsoft YaHei", 10))
        info.setMaximumHeight(150)
        info.setText(self.create_compare_text())
        layout.addWidget(info)

        self.setLayout(layout)

    @staticmethod
    def create_gray_histogram_image(gray_image: np.ndarray, title_text: str) -> np.ndarray:
        gray = gray_image.astype(np.uint8)
        hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).flatten()
        hist = hist / max(hist.max(), 1)

        width, height = 500, 360
        canvas = np.ones((height, width, 3), dtype=np.uint8) * 255
        margin_left = 50
        margin_right = 20
        margin_top = 40
        margin_bottom = 50
        plot_w = width - margin_left - margin_right
        plot_h = height - margin_top - margin_bottom

        cv2.line(canvas, (margin_left, margin_top), (margin_left, margin_top + plot_h), (0, 0, 0), 2)
        cv2.line(canvas, (margin_left, margin_top + plot_h), (margin_left + plot_w, margin_top + plot_h), (0, 0, 0), 2)

        bin_w = plot_w / 256
        for i in range(256):
            x1 = int(margin_left + i * bin_w)
            x2 = int(margin_left + (i + 1) * bin_w)
            bar_h = int(hist[i] * plot_h)
            y1 = margin_top + plot_h - bar_h
            y2 = margin_top + plot_h
            cv2.rectangle(canvas, (x1, y1), (max(x2, x1 + 1), y2), (60, 60, 60), -1)

        cv2.putText(canvas, title_text, (width // 2 - 55, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2)
        cv2.putText(canvas, "0", (margin_left - 5, height - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 1)
        cv2.putText(canvas, "255", (margin_left + plot_w - 35, height - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 1)
        cv2.putText(canvas, "Gray Value", (width // 2 - 55, height - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 1)
        cv2.putText(canvas, "Freq", (8, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 1)
        return canvas

    def create_compare_text(self) -> str:
        o = self.original_gray.astype(np.float32)
        p = self.processed_gray.astype(np.float32)
        return (
            "直方图对比说明：\n"
            f"原图平均灰度：{np.mean(o):.2f}，标准差：{np.std(o):.2f}；"
            f"处理后平均灰度：{np.mean(p):.2f}，标准差：{np.std(p):.2f}。\n"
            "灰度直方图可以反映图像像素分布变化。卡尔曼滤波通常会使灰度变化更平滑；"
            "Roberts和Sobel会突出边缘区域，使高灰度边缘像素比例发生变化；"
            "LBP会改变灰度编码分布，用于体现局部纹理特征。"
        )


class AnalysisReportDialog(QDialog):
    """
    显示并保存实验分析报告。
    """

    def __init__(self, report_text: str, default_name: str = "analysis_report.txt"):
        super().__init__()
        self.report_text = report_text
        self.default_name = default_name
        self.setWindowTitle("实验分析报告")
        self.resize(850, 650)
        self.init_ui()

    def init_ui(self) -> None:
        layout = QVBoxLayout()

        title = QLabel("医学图像处理实验分析报告")
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QFont("Microsoft YaHei", 16, QFont.Bold))
        layout.addWidget(title)

        self.text_edit = QTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setFont(QFont("Microsoft YaHei", 10))
        self.text_edit.setPlainText(self.report_text)
        layout.addWidget(self.text_edit)

        button_layout = QHBoxLayout()
        self.save_button = QPushButton("保存报告")
        self.close_button = QPushButton("关闭")
        self.save_button.setFont(QFont("Microsoft YaHei", 11))
        self.close_button.setFont(QFont("Microsoft YaHei", 11))
        self.save_button.clicked.connect(self.save_report)
        self.close_button.clicked.connect(self.close)
        button_layout.addStretch()
        button_layout.addWidget(self.save_button)
        button_layout.addWidget(self.close_button)
        layout.addLayout(button_layout)

        self.setLayout(layout)

    def save_report(self) -> None:
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "保存实验分析报告",
            self.default_name,
            "文本文件 (*.txt);;所有文件 (*.*)",
        )
        if not save_path:
            return
        try:
            with open(save_path, "w", encoding="utf-8") as f:
                f.write(self.text_edit.toPlainText())
            QMessageBox.information(self, "保存成功", f"实验分析报告已保存到：\n{save_path}")
        except Exception as e:
            QMessageBox.warning(self, "保存失败", f"报告保存失败：{e}")


# =========================================================
# 十、科大讯飞云语音识别后台线程
# =========================================================


class XfyunVoiceRecognitionWorker(threading.Thread):
    """
    科大讯飞云语音识别后台线程（本地VAD语音触发版）。

    这版不再每隔固定 3.2 秒录一次音，而是：
    1. 程序持续监听麦克风的音量；
    2. 检测到用户开始说话后才开始录音；
    3. 用户说完并停顿一小段时间后自动结束录音；
    4. 将这一整段语音上传到科大讯飞云端识别；
    5. 返回文字后交给主界面做指令匹配并执行功能。

    使用前需要设置环境变量：
    setx XFYUN_APPID "你的APPID"
    setx XFYUN_API_KEY "你的APIKey"
    setx XFYUN_API_SECRET "你的APISecret"
    """

    XFYUN_HOST = "iat-api.xfyun.cn"
    XFYUN_PATH = "/v2/iat"
    XFYUN_URL = "wss://iat-api.xfyun.cn/v2/iat"

    def __init__(
        self,
        command_signal,
        status_signal,
        max_record_seconds: float = 12.0,
        silence_seconds: float = 1.2,
    ):
        super().__init__(daemon=True)
        self.command_signal = command_signal
        self.status_signal = status_signal

        # 不再使用固定 3.2 秒录音。
        # max_record_seconds 表示一句口令最长允许录制多久，防止忘记停止导致一直录音。
        # silence_seconds 表示说完后静音多久自动结束录音。
        self.max_record_seconds = max_record_seconds
        self.silence_seconds = silence_seconds

        self.sample_rate = 16000
        self.channels = 1
        self.block_size = 1024  # 每次读取约 64ms 音频，适合做音量检测。
        self.pre_speech_seconds = 0.45  # 保留开始说话前的一小段音频，避免丢掉第一个字。
        self.min_voice_seconds = 0.20
        self.running = False

        # VAD音量阈值。程序启动语音控制后会先根据环境噪声自动校准。
        self.energy_threshold = 350.0
        self.noise_calibrated = False

        self.appid = os.getenv("XFYUN_APPID", "").strip()
        self.api_key = os.getenv("XFYUN_API_KEY", "").strip()
        self.api_secret = os.getenv("XFYUN_API_SECRET", "").strip()

    def stop(self) -> None:
        self.running = False

    def check_config(self) -> bool:
        """
        检查运行环境和讯飞密钥是否配置完整。
        """
        if not XFYUN_VOICE_AVAILABLE:
            self.status_signal.emit(
                "未安装讯飞语音依赖。请执行：pip install websocket-client sounddevice"
            )
            return False

        if not self.appid or not self.api_key or not self.api_secret:
            self.status_signal.emit(
                "未检测到讯飞密钥环境变量。请设置 XFYUN_APPID、XFYUN_API_KEY、XFYUN_API_SECRET 后重新启动程序。"
            )
            return False

        return True

    @staticmethod
    def calculate_rms(samples: np.ndarray) -> float:
        """
        计算一段 int16 音频的 RMS 音量，用于判断是否有人说话。
        """
        if samples is None or samples.size == 0:
            return 0.0
        samples_float = samples.astype(np.float32)
        return float(np.sqrt(np.mean(samples_float ** 2)))

    def calibrate_noise_threshold(self) -> None:
        """
        根据当前环境噪声自动设置说话检测阈值。
        作用：避免安静环境过敏，也避免嘈杂环境一直误触发。
        """
        try:
            self.status_signal.emit("语音状态：正在校准环境噪声，请保持安静约 1 秒...")
            frame_count = int(self.sample_rate * 1.0)
            noise_audio = sd.rec(
                frame_count,
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
            )
            sd.wait()
            noise_rms = self.calculate_rms(noise_audio.reshape(-1))

            # 阈值根据环境噪声自适应，同时设置上下限，避免过低或过高。
            self.energy_threshold = max(300.0, min(3000.0, noise_rms * 3.0 + 120.0))
            self.noise_calibrated = True
            self.status_signal.emit(
                f"语音状态：环境噪声校准完成，检测阈值={self.energy_threshold:.0f}。请说口令。"
            )
        except Exception as e:
            self.noise_calibrated = True
            self.energy_threshold = 500.0
            self.status_signal.emit(
                f"语音状态：环境噪声校准失败，使用默认阈值 500。原因：{e}"
            )

    def create_auth_url(self) -> str:
        """
        生成科大讯飞语音听写 WebSocket 鉴权 URL。
        """
        now = datetime.now()
        date = formatdate(timeval=mktime(now.timetuple()), localtime=False, usegmt=True)

        signature_origin = (
            f"host: {self.XFYUN_HOST}\n"
            f"date: {date}\n"
            f"GET {self.XFYUN_PATH} HTTP/1.1"
        )
        signature_sha = hmac.new(
            self.api_secret.encode("utf-8"),
            signature_origin.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        signature = base64.b64encode(signature_sha).decode("utf-8")

        authorization_origin = (
            f'api_key="{self.api_key}", algorithm="hmac-sha256", '
            f'headers="host date request-line", signature="{signature}"'
        )
        authorization = base64.b64encode(authorization_origin.encode("utf-8")).decode("utf-8")

        query = urlencode({
            "authorization": authorization,
            "date": date,
            "host": self.XFYUN_HOST,
        })
        return f"{self.XFYUN_URL}?{query}"

    def record_command_audio(self) -> bytes:
        """
        本地VAD录音：只有检测到用户开始说话后才录音，说完后自动结束。
        返回 PCM 16kHz、16bit、单声道原始字节。
        """
        if not self.noise_calibrated:
            self.calibrate_noise_threshold()

        pre_chunks = deque(
            maxlen=max(1, int(self.pre_speech_seconds * self.sample_rate / self.block_size))
        )
        voice_chunks: List[np.ndarray] = []
        speaking = False
        speech_start_time: Optional[float] = None
        silence_start_time: Optional[float] = None
        last_wait_status_time = 0.0

        try:
            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                blocksize=self.block_size,
            ) as stream:
                self.status_signal.emit(
                    f"语音状态：监听中，检测到你说话后才开始录音；说完停顿 {self.silence_seconds:.1f} 秒后自动识别。"
                )

                while self.running:
                    audio_block, overflowed = stream.read(self.block_size)
                    samples = audio_block.reshape(-1).copy()
                    rms = self.calculate_rms(samples)
                    now = time.time()

                    if not speaking:
                        pre_chunks.append(samples)

                        # 没说话时只等待，不上传到云端，也不会每隔固定秒数识别一次。
                        if rms >= self.energy_threshold:
                            speaking = True
                            speech_start_time = now
                            silence_start_time = None
                            voice_chunks.extend(list(pre_chunks))
                            pre_chunks.clear()
                            self.status_signal.emit("语音状态：检测到说话，正在录音...")
                        else:
                            # 长时间等待时偶尔刷新状态，避免用户以为程序卡住。
                            if now - last_wait_status_time > 6.0:
                                self.status_signal.emit("语音状态：监听中，请直接说口令。")
                                last_wait_status_time = now
                            continue

                    else:
                        voice_chunks.append(samples)

                        if rms >= self.energy_threshold:
                            silence_start_time = None
                        else:
                            if silence_start_time is None:
                                silence_start_time = now
                            elif now - silence_start_time >= self.silence_seconds:
                                break

                        if speech_start_time is not None and now - speech_start_time >= self.max_record_seconds:
                            self.status_signal.emit(
                                f"语音状态：已达到最长录音 {self.max_record_seconds:.0f} 秒，自动开始识别。"
                            )
                            break

            if not voice_chunks:
                return b""

            audio = np.concatenate(voice_chunks).astype(np.int16)
            duration = len(audio) / self.sample_rate

            if duration < self.min_voice_seconds:
                self.status_signal.emit("语音状态：检测到的声音太短，已忽略。")
                return b""

            self.status_signal.emit(f"语音状态：录音结束，语音长度约 {duration:.1f} 秒，正在上传识别...")
            return audio.tobytes()

        except Exception as e:
            self.status_signal.emit(f"语音录音出错：{e}")
            return b""

    @staticmethod
    def parse_xfyun_result(message: str) -> Tuple[str, bool, Optional[str]]:
        """
        解析讯飞 WebSocket 返回消息。

        返回：
        text: 当前片段识别文本
        is_final: 是否为最终状态
        error: 错误信息，没有错误时为 None
        """
        try:
            data = json.loads(message)
        except Exception as e:
            return "", False, f"返回结果解析失败：{e}"

        code = data.get("code", 0)
        if code != 0:
            return "", True, f"讯飞接口错误：code={code}, message={data.get('message', '')}"

        text = ""
        result = data.get("data", {}).get("result", {})
        for ws_item in result.get("ws", []):
            for cw_item in ws_item.get("cw", []):
                text += cw_item.get("w", "")

        is_final = data.get("data", {}).get("status", 0) == 2
        return text, is_final, None

    def recognize_once_by_xfyun(self, audio_bytes: bytes) -> str:
        """
        将一段 PCM 音频上传到讯飞云，并返回识别文本。
        """
        request_url = self.create_auth_url()
        ws = websocket.create_connection(
            request_url,
            sslopt={"cert_reqs": ssl.CERT_NONE},
            timeout=8,
        )

        try:
            frame_size = 1280  # 16k 16bit 下约 40ms 音频
            frames = [audio_bytes[i:i + frame_size] for i in range(0, len(audio_bytes), frame_size)]
            if not frames:
                return ""

            full_text_parts: List[str] = []

            for index, frame in enumerate(frames):
                if not self.running:
                    break

                if index == 0:
                    status = 0
                    payload = {
                        "common": {"app_id": self.appid},
                        "business": {
                            "language": "zh_cn",
                            "domain": "iat",
                            "accent": "mandarin",
                            "vinfo": 1,
                            # 云端VAD也适当放宽，避免稍微停顿就提前结束。
                            "vad_eos": 2000,
                        },
                        "data": {
                            "status": status,
                            "format": "audio/L16;rate=16000",
                            "encoding": "raw",
                            "audio": base64.b64encode(frame).decode("utf-8"),
                        },
                    }
                else:
                    status = 1
                    payload = {
                        "data": {
                            "status": status,
                            "format": "audio/L16;rate=16000",
                            "encoding": "raw",
                            "audio": base64.b64encode(frame).decode("utf-8"),
                        }
                    }

                ws.send(json.dumps(payload, ensure_ascii=False))
                time.sleep(0.04)

                # 尽量读取阶段性返回，避免服务端缓存过多。
                ws.settimeout(0.02)
                while True:
                    try:
                        message = ws.recv()
                    except Exception:
                        break
                    text, is_final, error = self.parse_xfyun_result(message)
                    if error:
                        raise RuntimeError(error)
                    if text:
                        full_text_parts.append(text)
                    if is_final:
                        return "".join(full_text_parts).strip()

            # 发送结束帧。
            end_payload = {
                "data": {
                    "status": 2,
                    "format": "audio/L16;rate=16000",
                    "encoding": "raw",
                    "audio": "",
                }
            }
            ws.send(json.dumps(end_payload, ensure_ascii=False))

            ws.settimeout(5)
            while True:
                message = ws.recv()
                text, is_final, error = self.parse_xfyun_result(message)
                if error:
                    raise RuntimeError(error)
                if text:
                    full_text_parts.append(text)
                if is_final:
                    break

            return "".join(full_text_parts).strip()

        finally:
            try:
                ws.close()
            except Exception:
                pass

    def run(self) -> None:
        if not self.check_config():
            return

        self.running = True
        self.status_signal.emit(
            "讯飞云语音控制已开启。现在是VAD语音触发模式：不再固定3.2秒识别一次，说话后才会录音识别。"
        )

        while self.running:
            try:
                audio_bytes = self.record_command_audio()

                if not self.running:
                    break

                if not audio_bytes:
                    time.sleep(0.1)
                    continue

                command_text = self.recognize_once_by_xfyun(audio_bytes)

                if command_text:
                    self.command_signal.emit(command_text)
                else:
                    self.status_signal.emit("讯飞未返回有效文字，请使用短口令重试。")

            except Exception as e:
                if self.running:
                    self.status_signal.emit(f"讯飞语音识别出错：{e}")
                    time.sleep(1.0)

        self.status_signal.emit("讯飞云语音控制已关闭。")


# =========================================================
# 十、主窗口
# =========================================================


class MedicalImageProcessingSystem(QMainWindow):
    """
    医学图像处理综合应用系统主界面。
    """

    voice_command_signal = pyqtSignal(str)
    voice_status_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()

        # 图像相关变量
        self.original_image: Optional[np.ndarray] = None
        self.display_original_image: Optional[np.ndarray] = None
        self.gray_image: Optional[np.ndarray] = None
        self.processed_image: Optional[np.ndarray] = None
        self.current_image_path: str = ""

        # 文件夹图像列表
        self.image_files: List[str] = []
        self.current_index: int = -1

        # 当前算法与交互状态
        self.current_algorithm_name: str = "未选择算法"
        self.last_process_time: float = 0.0
        self.save_status: str = "未保存"
        self.current_roi: Optional[Tuple[int, int, int, int]] = None

        # DICOM医学影像相关变量
        self.dicom_metadata: dict = {"is_dicom": False}
        self.current_window_center: Optional[float] = None
        self.current_window_width: Optional[float] = None
        self._updating_window_sliders: bool = False

        # 实时滑块刷新定时器：避免拖动时过于频繁计算导致卡顿
        self.realtime_timer = QTimer(self)
        self.realtime_timer.setSingleShot(True)
        self.realtime_timer.timeout.connect(self.reprocess_current_algorithm)

        # 语音控制相关变量
        self.voice_worker: Optional[XfyunVoiceRecognitionWorker] = None
        self.voice_enabled: bool = False
        self.build_voice_commands()
        self.voice_command_signal.connect(self.handle_voice_command)
        self.voice_status_signal.connect(self.update_voice_status)

        self.setWindowTitle("医学图像处理综合应用系统")
        self.resize(1280, 900)
        self.init_ui()
        self.update_info_panel()
        self.center_window()

    # -----------------------------------------------------
    # 界面初始化
    # -----------------------------------------------------
    def init_ui(self) -> None:
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        main_layout = QVBoxLayout()
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(12, 10, 12, 10)

        title_label = QLabel("医学图像处理综合应用系统")
        title_label.setAlignment(Qt.AlignCenter)
        title_label.setFont(QFont("Microsoft YaHei", 20, QFont.Bold))
        main_layout.addWidget(title_label)

        # 顶部：选择图像、选择文件夹、上一张、下一张
        top_layout = QHBoxLayout()
        self.choose_image_button = QPushButton("选择原图像")
        self.open_dicom_button = QPushButton("打开DICOM")
        self.choose_folder_button = QPushButton("选择文件夹")
        self.prev_button = QPushButton("上一张")
        self.next_button = QPushButton("下一张")
        self.image_counter_label = QLabel("当前：0 / 0")

        top_buttons = [self.choose_image_button, self.open_dicom_button, self.choose_folder_button, self.prev_button, self.next_button]
        for btn in top_buttons:
            btn.setFont(QFont("Microsoft YaHei", 11))
            btn.setMinimumHeight(38)

        self.image_counter_label.setFont(QFont("Microsoft YaHei", 11))
        self.image_counter_label.setAlignment(Qt.AlignCenter)

        self.choose_image_button.clicked.connect(self.choose_single_image)
        self.open_dicom_button.clicked.connect(self.choose_dicom_image)
        self.choose_folder_button.clicked.connect(self.choose_image_folder)
        self.prev_button.clicked.connect(self.show_previous_image)
        self.next_button.clicked.connect(self.show_next_image)

        top_layout.addWidget(self.choose_image_button)
        top_layout.addWidget(self.open_dicom_button)
        top_layout.addWidget(self.choose_folder_button)
        top_layout.addWidget(self.prev_button)
        top_layout.addWidget(self.next_button)
        top_layout.addWidget(self.image_counter_label)
        main_layout.addLayout(top_layout)

        self.path_label = QLabel("当前未选择图像")
        self.path_label.setFont(QFont("Microsoft YaHei", 9))
        self.path_label.setStyleSheet("color: #555555;")
        main_layout.addWidget(self.path_label)

        # 中间：固定尺寸图像显示区
        image_layout = QHBoxLayout()
        image_layout.setAlignment(Qt.AlignCenter)

        left_frame = QFrame()
        left_frame.setFrameShape(QFrame.Box)
        left_frame.setLineWidth(1)
        left_layout = QVBoxLayout()
        left_title = QLabel("原图像")
        left_title.setAlignment(Qt.AlignCenter)
        left_title.setFont(QFont("Microsoft YaHei", 14, QFont.Bold))
        self.original_label = StableImageLabel("请先选择原图像或图像文件夹", 580, 430)
        left_layout.addWidget(left_title)
        left_layout.addWidget(self.original_label, alignment=Qt.AlignCenter)
        left_frame.setLayout(left_layout)

        right_frame = QFrame()
        right_frame.setFrameShape(QFrame.Box)
        right_frame.setLineWidth(1)
        right_layout = QVBoxLayout()
        right_title = QLabel("处理后的图像")
        right_title.setAlignment(Qt.AlignCenter)
        right_title.setFont(QFont("Microsoft YaHei", 14, QFont.Bold))
        self.processed_label = StableImageLabel("请选择下方算法进行处理", 580, 430)
        right_layout.addWidget(right_title)
        right_layout.addWidget(self.processed_label, alignment=Qt.AlignCenter)
        right_frame.setLayout(right_layout)

        image_layout.addWidget(left_frame)
        image_layout.addWidget(right_frame)
        main_layout.addLayout(image_layout)

        # 算法按钮区域
        algorithm_layout = QGridLayout()
        self.kalman_button = QPushButton("卡尔曼滤波")
        self.roberts_button = QPushButton("Roberts算子")
        self.sobel_button = QPushButton("Sobel梯度")
        self.lbp_button = QPushButton("LBP局部二值模式")
        self.canny_button = QPushButton("Canny边缘检测")
        self.otsu_button = QPushButton("Otsu分割+形态学轮廓")
        self.pseudo_color_button = QPushButton("伪彩色增强")

        algorithm_buttons = [
            self.kalman_button, self.roberts_button, self.sobel_button, self.lbp_button,
            self.canny_button, self.otsu_button, self.pseudo_color_button
        ]
        for btn in algorithm_buttons:
            btn.setFont(QFont("Microsoft YaHei", 12))
            btn.setMinimumHeight(42)
            btn.setStyleSheet(
                """
                QPushButton {
                    background-color: #E8F0FE;
                    border: 1px solid #5B8DEF;
                    border-radius: 6px;
                    color: #222222;
                }
                QPushButton:hover { background-color: #D2E3FC; }
                QPushButton:pressed { background-color: #AECBFA; }
                """
            )

        self.kalman_button.clicked.connect(lambda: self.apply_algorithm("卡尔曼滤波"))
        self.roberts_button.clicked.connect(lambda: self.apply_algorithm("Roberts算子"))
        self.sobel_button.clicked.connect(lambda: self.apply_algorithm("Sobel梯度"))
        self.lbp_button.clicked.connect(lambda: self.apply_algorithm("LBP局部二值模式"))
        self.canny_button.clicked.connect(lambda: self.apply_algorithm("Canny边缘检测"))
        self.otsu_button.clicked.connect(lambda: self.apply_algorithm("Otsu分割+形态学轮廓"))
        self.pseudo_color_button.clicked.connect(lambda: self.apply_algorithm("伪彩色增强"))

        algorithm_layout.addWidget(self.kalman_button, 0, 0)
        algorithm_layout.addWidget(self.roberts_button, 0, 1)
        algorithm_layout.addWidget(self.sobel_button, 0, 2)
        algorithm_layout.addWidget(self.lbp_button, 0, 3)
        algorithm_layout.addWidget(self.canny_button, 1, 0)
        algorithm_layout.addWidget(self.otsu_button, 1, 1, 1, 2)
        algorithm_layout.addWidget(self.pseudo_color_button, 1, 3)
        main_layout.addLayout(algorithm_layout)

        # 参数滑块区域
        parameter_layout = QHBoxLayout()
        parameter_title = QLabel("参数调节：")
        parameter_title.setFont(QFont("Microsoft YaHei", 11, QFont.Bold))

        self.parameter_slider = QSlider(Qt.Horizontal)
        self.parameter_slider.setRange(1, 100)
        self.parameter_slider.setValue(50)
        self.parameter_slider.setTickPosition(QSlider.TicksBelow)
        self.parameter_slider.setTickInterval(10)
        self.parameter_slider.valueChanged.connect(self.on_parameter_changed)

        self.parameter_value_label = QLabel("当前参数值：50")
        self.parameter_value_label.setFont(QFont("Microsoft YaHei", 10))
        self.parameter_value_label.setMinimumWidth(120)

        self.reprocess_button = QPushButton("重新处理")
        self.reprocess_button.setFont(QFont("Microsoft YaHei", 10))
        self.reprocess_button.setMinimumHeight(34)
        self.reprocess_button.clicked.connect(self.reprocess_current_algorithm)

        parameter_layout.addWidget(parameter_title)
        parameter_layout.addWidget(self.parameter_slider)
        parameter_layout.addWidget(self.parameter_value_label)
        parameter_layout.addWidget(self.reprocess_button)
        main_layout.addLayout(parameter_layout)

        self.parameter_hint_label = QLabel("参数说明：卡尔曼滤波控制平滑强度；Roberts/Sobel/Canny控制边缘阈值；LBP控制纹理显示增强；伪彩色参数控制色彩映射风格。")
        self.parameter_hint_label.setFont(QFont("Microsoft YaHei", 9))
        self.parameter_hint_label.setStyleSheet("color: #555555;")
        main_layout.addWidget(self.parameter_hint_label)

        # DICOM窗宽窗位调节：只在打开DICOM图像后启用
        dicom_layout = QHBoxLayout()
        dicom_title = QLabel("DICOM窗宽窗位：")
        dicom_title.setFont(QFont("Microsoft YaHei", 10, QFont.Bold))
        self.window_width_slider = QSlider(Qt.Horizontal)
        self.window_center_slider = QSlider(Qt.Horizontal)
        self.window_width_slider.setRange(1, 4000)
        self.window_center_slider.setRange(-1000, 3000)
        self.window_width_slider.setValue(400)
        self.window_center_slider.setValue(40)
        self.window_width_slider.setEnabled(False)
        self.window_center_slider.setEnabled(False)
        self.window_width_slider.valueChanged.connect(self.on_dicom_window_changed)
        self.window_center_slider.valueChanged.connect(self.on_dicom_window_changed)
        self.window_width_label = QLabel("窗宽：未启用")
        self.window_center_label = QLabel("窗位：未启用")
        for lab in [self.window_width_label, self.window_center_label]:
            lab.setFont(QFont("Microsoft YaHei", 9))
            lab.setMinimumWidth(95)
        dicom_layout.addWidget(dicom_title)
        dicom_layout.addWidget(self.window_width_label)
        dicom_layout.addWidget(self.window_width_slider)
        dicom_layout.addWidget(self.window_center_label)
        dicom_layout.addWidget(self.window_center_slider)
        main_layout.addLayout(dicom_layout)

        # 语音控制区域
        voice_layout = QHBoxLayout()
        voice_title = QLabel("语音控制：")
        voice_title.setFont(QFont("Microsoft YaHei", 11, QFont.Bold))

        self.start_voice_button = QPushButton("开启语音控制")
        self.stop_voice_button = QPushButton("关闭语音控制")
        self.voice_help_button = QPushButton("语音指令帮助")

        for btn in [self.start_voice_button, self.stop_voice_button, self.voice_help_button]:
            btn.setFont(QFont("Microsoft YaHei", 10))
            btn.setMinimumHeight(34)

        self.start_voice_button.clicked.connect(self.start_voice_control)
        self.stop_voice_button.clicked.connect(self.stop_voice_control)
        self.voice_help_button.clicked.connect(self.show_voice_help)

        self.voice_status_label = QLabel("语音状态：未开启。设置讯飞环境变量后，可把图片放入项目 images 文件夹，说“打开默认文件夹”开始无鼠标演示。")
        self.voice_status_label.setFont(QFont("Microsoft YaHei", 9))
        self.voice_status_label.setStyleSheet("color: #444444;")
        self.voice_status_label.setWordWrap(True)

        voice_layout.addWidget(voice_title)
        voice_layout.addWidget(self.start_voice_button)
        voice_layout.addWidget(self.stop_voice_button)
        voice_layout.addWidget(self.voice_help_button)
        voice_layout.addWidget(self.voice_status_label, stretch=1)
        main_layout.addLayout(voice_layout)

        # 扩展交互功能按钮
        extra_layout = QHBoxLayout()
        self.compare_button = QPushButton("一键对比")
        self.roi_button = QPushButton("选择ROI区域")
        self.clear_roi_button = QPushButton("清除ROI")
        self.lbp_hist_button = QPushButton("显示LBP直方图")
        self.gray_hist_button = QPushButton("灰度直方图对比")

        extra_buttons = [
            self.compare_button,
            self.roi_button,
            self.clear_roi_button,
            self.lbp_hist_button,
            self.gray_hist_button,
        ]
        for btn in extra_buttons:
            btn.setFont(QFont("Microsoft YaHei", 11))
            btn.setMinimumHeight(38)

        self.compare_button.clicked.connect(self.open_compare_dialog)
        self.roi_button.clicked.connect(self.select_roi_region)
        self.clear_roi_button.clicked.connect(self.clear_roi_region)
        self.lbp_hist_button.clicked.connect(self.open_lbp_histogram)
        self.gray_hist_button.clicked.connect(self.open_gray_histogram_compare)

        extra_layout.addWidget(self.compare_button)
        extra_layout.addWidget(self.roi_button)
        extra_layout.addWidget(self.clear_roi_button)
        extra_layout.addWidget(self.lbp_hist_button)
        extra_layout.addWidget(self.gray_hist_button)
        main_layout.addLayout(extra_layout)

        # 创新功能按钮区域：分析报告 + 组合处理流程
        innovation_layout = QHBoxLayout()
        self.analysis_report_button = QPushButton("一键生成实验分析报告")
        self.combo_sobel_button = QPushButton("卡尔曼+Sobel")
        self.combo_roberts_button = QPushButton("卡尔曼+Roberts")
        self.combo_lbp_button = QPushButton("卡尔曼+LBP")

        innovation_buttons = [
            self.analysis_report_button,
            self.combo_sobel_button,
            self.combo_roberts_button,
            self.combo_lbp_button,
        ]
        for btn in innovation_buttons:
            btn.setFont(QFont("Microsoft YaHei", 11))
            btn.setMinimumHeight(38)
            btn.setStyleSheet(
                """
                QPushButton {
                    background-color: #FFF7E6;
                    border: 1px solid #F6B26B;
                    border-radius: 6px;
                    color: #222222;
                }
                QPushButton:hover { background-color: #FFE8B3; }
                QPushButton:pressed { background-color: #FFD27A; }
                """
            )

        self.analysis_report_button.clicked.connect(self.open_analysis_report)
        self.combo_sobel_button.clicked.connect(lambda: self.apply_algorithm("卡尔曼滤波 + Sobel梯度"))
        self.combo_roberts_button.clicked.connect(lambda: self.apply_algorithm("卡尔曼滤波 + Roberts算子"))
        self.combo_lbp_button.clicked.connect(lambda: self.apply_algorithm("卡尔曼滤波 + LBP局部二值模式"))

        innovation_layout.addWidget(self.analysis_report_button)
        innovation_layout.addWidget(self.combo_sobel_button)
        innovation_layout.addWidget(self.combo_roberts_button)
        innovation_layout.addWidget(self.combo_lbp_button)
        main_layout.addLayout(innovation_layout)

        # 高级技术性功能按钮区域
        technical_layout = QHBoxLayout()
        self.roi_analysis_button = QPushButton("ROI定量分析")
        self.quality_button = QPushButton("图像质量评价")
        self.glcm_button = QPushButton("GLCM纹理特征")
        self.batch_button = QPushButton("批量处理+CSV导出")

        technical_buttons = [self.roi_analysis_button, self.quality_button, self.glcm_button, self.batch_button]
        for btn in technical_buttons:
            btn.setFont(QFont("Microsoft YaHei", 11))
            btn.setMinimumHeight(38)
            btn.setStyleSheet(
                """
                QPushButton {
                    background-color: #EAF7F0;
                    border: 1px solid #36A269;
                    border-radius: 6px;
                    color: #222222;
                }
                QPushButton:hover { background-color: #D7F0E2; }
                QPushButton:pressed { background-color: #BFE4D0; }
                """
            )

        self.roi_analysis_button.clicked.connect(self.open_roi_analysis)
        self.quality_button.clicked.connect(self.open_quality_evaluation)
        self.glcm_button.clicked.connect(self.open_glcm_features)
        self.batch_button.clicked.connect(self.batch_process_and_export)

        technical_layout.addWidget(self.roi_analysis_button)
        technical_layout.addWidget(self.quality_button)
        technical_layout.addWidget(self.glcm_button)
        technical_layout.addWidget(self.batch_button)
        main_layout.addLayout(technical_layout)

        # 底部：打开与保存
        bottom_layout = QHBoxLayout()
        self.open_button = QPushButton("打开")
        self.save_button = QPushButton("保存")

        for btn in [self.open_button, self.save_button]:
            btn.setFont(QFont("Microsoft YaHei", 12))
            btn.setMinimumHeight(42)

        self.open_button.clicked.connect(self.open_large_preview)
        self.save_button.clicked.connect(self.save_processed_image)

        bottom_layout.addWidget(self.open_button)
        bottom_layout.addStretch()
        bottom_layout.addWidget(self.save_button)
        main_layout.addLayout(bottom_layout)

        # 信息栏
        self.info_label = QLabel()
        self.info_label.setFont(QFont("Microsoft YaHei", 9))
        self.info_label.setStyleSheet("background-color: #F5F5F5; border: 1px solid #DDDDDD; padding: 6px;")
        self.info_label.setWordWrap(True)
        main_layout.addWidget(self.info_label)

        central_widget.setLayout(main_layout)

    # -----------------------------------------------------
    # 通用检查与加载
    # -----------------------------------------------------
    def check_image_loaded(self) -> bool:
        if self.original_image is None:
            QMessageBox.information(self, "提示", "请先选择原图像或图像文件夹。")
            return False
        return True

    def load_image_by_path(self, file_path: str) -> bool:
        image, metadata = read_medical_image(file_path)
        if image is None:
            error_message = metadata.get("error", "图像读取失败") if isinstance(metadata, dict) else "图像读取失败"
            QMessageBox.warning(self, "读取失败", f"{error_message}：\n{file_path}")
            return False

        self.original_image = image
        self.gray_image = convert_to_gray(image)
        self.processed_image = None
        self.current_image_path = file_path
        self.current_algorithm_name = "未选择算法"
        self.last_process_time = 0.0
        self.save_status = "未保存"
        self.current_roi = None
        self.dicom_metadata = metadata or {"is_dicom": False}

        self.configure_dicom_window_controls()

        self.display_original_image = self.original_image.copy()
        self.path_label.setText(file_path)
        self.processed_label.setPixmap(QPixmap())
        self.processed_label.setText("请选择下方算法进行处理")
        self.show_original_image()
        self.update_counter_label()
        self.update_info_panel()
        return True

    def configure_dicom_window_controls(self) -> None:
        """根据当前图像是否为DICOM启用或关闭窗宽窗位滑块。"""
        is_dicom = bool(self.dicom_metadata.get("is_dicom", False))
        self.window_width_slider.setEnabled(is_dicom)
        self.window_center_slider.setEnabled(is_dicom)

        if not is_dicom:
            self.window_width_label.setText("窗宽：未启用")
            self.window_center_label.setText("窗位：未启用")
            self.current_window_width = None
            self.current_window_center = None
            return

        self.current_window_width = float(self.dicom_metadata.get("window_width", 400.0))
        self.current_window_center = float(self.dicom_metadata.get("window_center", 40.0))
        min_v = int(np.floor(self.dicom_metadata.get("min_value", -1000)))
        max_v = int(np.ceil(self.dicom_metadata.get("max_value", 3000)))
        ww_max = max(10, int(max_v - min_v))

        self._updating_window_sliders = True
        self.window_width_slider.setRange(1, max(ww_max, 10))
        self.window_center_slider.setRange(min_v, max_v)
        self.window_width_slider.setValue(int(np.clip(self.current_window_width, 1, max(ww_max, 10))))
        self.window_center_slider.setValue(int(np.clip(self.current_window_center, min_v, max_v)))
        self._updating_window_sliders = False

        self.window_width_label.setText(f"窗宽：{int(self.window_width_slider.value())}")
        self.window_center_label.setText(f"窗位：{int(self.window_center_slider.value())}")

    def on_dicom_window_changed(self) -> None:
        """DICOM窗宽窗位变化后实时重新显示图像。"""
        if self._updating_window_sliders or not bool(self.dicom_metadata.get("is_dicom", False)):
            return
        raw = self.dicom_metadata.get("raw_pixels")
        if raw is None:
            return

        ww = int(self.window_width_slider.value())
        wc = int(self.window_center_slider.value())
        self.window_width_label.setText(f"窗宽：{ww}")
        self.window_center_label.setText(f"窗位：{wc}")
        self.current_window_width = float(ww)
        self.current_window_center = float(wc)

        gray = dicom_pixels_to_uint8(raw, wc, ww)
        self.original_image = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        self.gray_image = gray
        self.processed_image = None
        self.current_algorithm_name = "未选择算法"
        self.save_status = "未保存"
        self.processed_label.setPixmap(QPixmap())
        self.processed_label.setText("窗宽窗位已更新，请选择算法处理")
        self.show_original_image()
        self.update_info_panel()

    def show_original_image(self) -> None:
        if self.original_image is None:
            return
        self.display_original_image = draw_roi_rectangle(self.original_image, self.current_roi)
        set_label_image(self.original_label, self.display_original_image)

    def show_processed_result(self) -> None:
        self.show_original_image()
        if self.processed_image is not None:
            set_label_image(self.processed_label, self.processed_image)

    # -----------------------------------------------------
    # 选择单张图片与文件夹
    # -----------------------------------------------------
    def choose_single_image(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择医学图像",
            "",
            "图像文件 (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.dcm)",
        )
        if not file_path:
            return

        self.image_files = [file_path]
        self.current_index = 0
        self.load_image_by_path(file_path)

    def choose_dicom_image(self) -> None:
        """专门选择DICOM医学影像文件，便于演示DICOM读取与窗宽窗位调节。"""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择DICOM医学影像",
            "",
            "DICOM文件 (*.dcm);;所有文件 (*.*)",
        )
        if not file_path:
            return
        self.image_files = [file_path]
        self.current_index = 0
        self.load_image_by_path(file_path)

    def choose_image_folder(self) -> None:
        folder_path = QFileDialog.getExistingDirectory(self, "选择医学图像文件夹", "")
        if not folder_path:
            return

        image_files = get_image_files_from_folder(folder_path)
        if len(image_files) == 0:
            QMessageBox.information(self, "提示", "该文件夹中没有找到支持格式的图像。")
            return

        self.image_files = image_files
        self.current_index = 0
        self.load_image_by_path(self.image_files[self.current_index])

    def load_default_image_folder(self) -> None:
        """
        读取项目目录下的 images 文件夹。
        这个功能主要用于语音控制演示：提前把图片放到 images 文件夹，
        运行程序后说“打开默认文件夹”，即可不通过鼠标选择文件夹。
        """
        default_folder = os.path.join(os.getcwd(), "images")
        if not os.path.isdir(default_folder):
            QMessageBox.information(
                self,
                "提示",
                f"没有找到默认 images 文件夹。请在当前项目目录下建立 images 文件夹，并放入医学图像。\n\n当前查找路径：\n{default_folder}",
            )
            self.update_voice_status("未找到默认 images 文件夹。")
            return

        image_files = get_image_files_from_folder(default_folder)
        if len(image_files) == 0:
            QMessageBox.information(self, "提示", "默认 images 文件夹中没有找到支持格式的图像。")
            self.update_voice_status("默认 images 文件夹中没有图像。")
            return

        self.image_files = image_files
        self.current_index = 0
        self.load_image_by_path(self.image_files[self.current_index])
        self.update_voice_status(f"已加载默认文件夹，共 {len(image_files)} 张图像。")

    def show_previous_image(self) -> None:
        if len(self.image_files) == 0:
            QMessageBox.information(self, "提示", "请先选择图像文件夹。")
            return
        self.current_index = (self.current_index - 1) % len(self.image_files)
        self.load_image_by_path(self.image_files[self.current_index])

    def show_next_image(self) -> None:
        if len(self.image_files) == 0:
            QMessageBox.information(self, "提示", "请先选择图像文件夹。")
            return
        self.current_index = (self.current_index + 1) % len(self.image_files)
        self.load_image_by_path(self.image_files[self.current_index])

    def update_counter_label(self) -> None:
        if len(self.image_files) == 0 or self.current_index < 0:
            self.image_counter_label.setText("当前：0 / 0")
        else:
            self.image_counter_label.setText(f"当前：{self.current_index + 1} / {len(self.image_files)}")

    # -----------------------------------------------------
    # 算法执行与实时参数变化
    # -----------------------------------------------------
    def get_parameter_value(self) -> int:
        return int(self.parameter_slider.value())

    def on_parameter_changed(self, value: int) -> None:
        self.parameter_value_label.setText(f"当前参数值：{value}")

        # 已经选择算法并且已经加载图像时，滑块变化后自动重新处理
        if self.original_image is not None and self.current_algorithm_name != "未选择算法":
            self.realtime_timer.start(120)

    def apply_algorithm(self, algorithm_name: str) -> None:
        if not self.check_image_loaded():
            return

        self.current_algorithm_name = algorithm_name
        self.reprocess_current_algorithm()

    def reprocess_current_algorithm(self) -> None:
        if self.original_image is None:
            return

        if self.current_algorithm_name == "未选择算法":
            QMessageBox.information(self, "提示", "请先点击一种算法按钮。")
            return

        start_time = time.time()
        QApplication.processEvents()

        parameter_value = self.get_parameter_value()
        self.processed_image = process_with_optional_roi(
            self.gray_image,
            self.current_roi,
            self.current_algorithm_name,
            parameter_value,
        )

        self.last_process_time = time.time() - start_time
        self.save_status = "未保存"
        self.show_processed_result()
        self.update_info_panel()

    # -----------------------------------------------------
    # 语音控制功能
    # -----------------------------------------------------
    def start_voice_control(self) -> None:
        """
        开启语音控制。
        """
        if self.voice_enabled and self.voice_worker is not None and self.voice_worker.running:
            self.update_voice_status("语音控制已经处于开启状态。")
            return

        if not XFYUN_VOICE_AVAILABLE:
            QMessageBox.warning(
                self,
                "讯飞语音依赖未安装",
                "当前环境未安装科大讯飞云语音所需依赖。\n\n请在终端执行：\npip install websocket-client sounddevice\n\n并确认已设置环境变量：\nXFYUN_APPID、XFYUN_API_KEY、XFYUN_API_SECRET",
            )
            return

        self.voice_worker = XfyunVoiceRecognitionWorker(self.voice_command_signal, self.voice_status_signal)
        self.voice_enabled = True
        self.voice_worker.start()
        self.update_voice_status("讯飞云语音控制正在启动。")

    def stop_voice_control(self) -> None:
        """
        关闭语音控制。
        """
        if self.voice_worker is not None:
            self.voice_worker.stop()
        self.voice_enabled = False
        self.update_voice_status("正在关闭语音控制。")

    def update_voice_status(self, text: str) -> None:
        """
        更新语音状态显示。
        """
        if hasattr(self, "voice_status_label"):
            self.voice_status_label.setText(f"语音状态：{text}")

    def build_voice_commands(self) -> None:
        """
        建立语音指令词库。
        一个功能对应多个常见说法，用来降低语音识别不准确带来的影响。
        """
        self.voice_command_dict = {
            "open_default_folder": [
                "打开默认文件夹", "读取默认文件夹", "打开图片文件夹", "读取图片", "加载图片",
                "加载图像", "打开图像库", "默认文件夹", "载入默认文件夹"
            ],
            "choose_image": [
                "选择图像", "打开图像", "选择图片", "打开图片", "导入图像", "导入图片"
            ],
            "choose_folder": [
                "选择文件夹", "打开文件夹", "导入文件夹", "读取文件夹"
            ],
            "previous_image": [
                "上一张", "上一个", "上一幅", "上一页", "前一张", "前一个", "上张图片"
            ],
            "next_image": [
                "下一张", "下一个", "下一幅", "下一页", "后一张", "后一个", "下张图片"
            ],
            "kalman": [
                "卡尔曼滤波", "卡尔曼", "卡尔门", "滤波", "去噪", "图像去噪", "平滑",
                "平滑处理", "卡尔曼去噪", "卡门滤波"
            ],
            "roberts": [
                "roberts", "robert", "罗伯茨", "罗伯特", "罗伯斯", "洛伯茨", "若伯茨",
                "罗伯茨算子", "roberts算子", "边缘一", "第一种边缘", "快速边缘", "交叉梯度"
            ],
            "sobel": [
                "sobel", "sober", "索贝尔", "搜贝尔", "索贝", "索贝儿", "索贝尔梯度",
                "sobel梯度", "梯度", "梯度检测", "边缘二", "第二种边缘", "边缘检测",
                # 以下是中文语音识别常把“梯度/索贝尔梯度”误识别出的近音词，
                # 例如用户说“梯度”，系统可能识别成“宜都一度”。
                "一度", "宜都", "易度", "医都", "遗都", "低度", "弟度", "提度",
                "剃度", "题度", "梯肚", "低都", "地度", "极度", "一堵", "一读"
            ],
            "lbp": [
                "lbp", "l b p", "局部二值模式", "二值模式", "纹理", "纹理分析", "纹理提取",
                "局部纹理", "二值纹理", "lbp纹理"
            ],
            "compare": [
                "一键对比", "算法对比", "对比", "显示对比", "全部结果", "显示全部", "四种算法",
                "四个结果", "结果对比"
            ],
            "histogram": [
                "显示直方图", "直方图", "lbp直方图", "纹理直方图", "显示纹理直方图", "查看直方图"
            ],
            "gray_hist_compare": [
                "灰度直方图", "灰度直方图对比", "处理前后直方图", "直方图对比", "灰度分布对比"
            ],
            "analysis_report": [
                "生成报告", "实验报告", "分析报告", "生成实验报告", "一键生成报告", "一键生成实验分析报告", "报告"
            ],
            "canny": [
                "canny", "canny边缘", "坎尼", "坎尼边缘", "高级边缘", "双阈值边缘"
            ],
            "otsu_segment": [
                "otsu", "大津", "大津分割", "图像分割", "自动分割", "形态学", "轮廓叠加", "分割轮廓"
            ],
            "pseudo_color": [
                "伪彩色", "伪彩色增强", "彩色增强", "彩色显示", "灰度转彩色"
            ],
            "glcm": [
                "glcm", "灰度共生矩阵", "共生矩阵", "纹理特征", "glcm纹理"
            ],
            "roi_analysis": [
                "roi分析", "roi定量分析", "区域分析", "局部分析", "感兴趣区域分析"
            ],
            "quality_eval": [
                "质量评价", "图像质量", "图像指标", "质量指标", "评价指标"
            ],
            "batch_export": [
                "批量处理", "批量导出", "批量保存", "导出csv", "导出表格", "批处理"
            ],
            "pipeline_sobel": [
                "组合sobel", "组合索贝尔", "卡尔曼加sobel", "卡尔曼加索贝尔", "卡尔曼加梯度", "组合梯度", "组合处理"
            ],
            "pipeline_roberts": [
                "组合roberts", "组合罗伯茨", "卡尔曼加roberts", "卡尔曼加罗伯茨", "组合罗伯特"
            ],
            "pipeline_lbp": [
                "组合lbp", "组合纹理", "卡尔曼加lbp", "卡尔曼加纹理", "组合局部二值模式"
            ],
            "preview": [
                "打开预览", "放大图像", "打开大图", "放大", "预览", "查看大图", "打开结果"
            ],
            "center_roi": [
                "中心roi", "中央roi", "自动roi", "选择roi", "感兴趣区域", "中心区域", "局部区域",
                "选择局部区域", "设置roi"
            ],
            "manual_roi": [
                "手动roi", "手动选择roi", "鼠标选择roi", "框选区域", "框选roi"
            ],
            "clear_roi": [
                "清除roi", "取消roi", "清除区域", "取消区域", "恢复全图", "不要roi"
            ],
            "increase_param": [
                "参数增大", "增大参数", "提高参数", "加大参数", "参数加十", "参数增加", "调大参数"
            ],
            "decrease_param": [
                "参数减小", "减小参数", "降低参数", "减少参数", "参数减十", "调小参数"
            ],
            "save": [
                "保存", "保存图像", "保存图片", "导出", "导出图像", "导出图片"
            ],
            "help": [
                "帮助", "指令帮助", "语音帮助", "语音指令", "有哪些指令"
            ],
            "stop_voice": [
                "关闭语音", "停止语音", "关闭语音控制", "停止识别"
            ],
            "exit": [
                "退出程序", "关闭程序", "退出", "结束程序"
            ],
        }

    def show_voice_help(self) -> None:
        """
        显示支持的语音指令。
        """
        help_text = (
            "支持的讯飞云语音指令：\n\n"
            "1. 图像读取：打开默认文件夹、选择图像、选择文件夹、打开DICOM\n"
            "2. 图像切换：上一张、下一张\n"
            "3. 基本算法：滤波、罗伯茨、索贝尔/梯度、纹理/LBP\n"
            "4. 扩展算法：Canny/坎尼、大津分割、伪彩色、GLCM\n"
            "5. 分析功能：ROI分析、质量评价、灰度直方图、生成报告、批量处理\n"
            "6. 结果展示：一键对比、直方图、放大、预览\n"
            "7. ROI功能：中心ROI、手动ROI、清除ROI\n"
            "8. 参数控制：参数增大、参数减小、参数设为50、参数80\n"
            "9. 文件操作：保存图像、关闭语音、退出程序\n\n"
            "识别不准时建议使用短口令：\n"
            "下一张、上一张、滤波、罗伯茨、梯度、纹理、坎尼、分割、伪彩色、质量评价、批量处理、保存。\n\n"
            "说明：如果想全程尽量不用鼠标，建议提前把医学图像放入项目目录下的 images 文件夹，"
            "然后开启讯飞云语音控制后说“打开默认文件夹”。"
        )
        QMessageBox.information(self, "语音指令帮助", help_text)

    def normalize_voice_text(self, command_text: str) -> str:
        """
        清洗语音识别文本：去掉空格、标点，并统一为小写。
        """
        if command_text is None:
            return ""
        text = command_text.lower()
        text = text.replace(" ", "")
        text = re.sub(r"[，。,.！!？?、：:；;（）()\[\]【】]", "", text)
        return text

    def match_by_homophone_rule(self, text: str) -> Tuple[Optional[str], float, str]:
        """
        语音近音容错规则。
        作用：当识别文本和正确关键词字面差异很大、difflib 分数很低时，
        仍然可以根据常见误识别词执行正确命令。
        例如：用户说“梯度”，识别结果可能是“宜都一度”，普通模糊匹配只有0.33，
        这里可以直接映射到 Sobel 梯度。
        """
        homophone_rules = {
            "sobel": [
                "宜都一度", "宜都", "一度", "易度", "医都", "遗都", "低度",
                "弟度", "提度", "剃度", "题度", "梯肚", "低都", "地度",
                "一堵", "一读", "索贝儿", "搜贝", "搜贝尔", "锁贝尔"
            ],
            "roberts": [
                "萝卜", "萝卜丝", "罗伯斯", "罗伯特", "罗伯茨", "洛伯茨",
                "若伯茨", "罗布斯", "肉伯茨"
            ],
            "kalman": [
                "卡门", "卡尔门", "卡尔曼", "卡尔曼滤", "卡尔曼旅波",
                "卡门滤波", "去燥", "去躁", "滤播"
            ],
            "lbp": [
                "阿尔比", "二值", "二指", "局部二值", "局部二指",
                "纹里", "文理", "纹理"
            ],
            "compare": [
                "一起对比", "一集对比", "一键堆比", "算法堆比", "全部显示"
            ],
            "histogram": [
                "直方", "直方土", "直方图", "方图", "纹理直方"
            ],
            "next_image": [
                "下一", "下一个", "下一副", "下一幅", "下页", "后一张"
            ],
            "previous_image": [
                "上一", "上一个", "上一副", "上一幅", "上页", "前一张"
            ],
        }

        for command, words in homophone_rules.items():
            for word in words:
                word_clean = self.normalize_voice_text(word)
                if word_clean and word_clean in text:
                    return command, 0.72, f"{word}（近音容错）"

        return None, 0.0, ""

    def match_voice_command(self, command_text: str) -> Tuple[Optional[str], float, str]:
        """
        根据语音识别文本匹配最接近的功能指令。
        返回：命令名称、匹配分数、匹配到的关键词。
        """
        text = self.normalize_voice_text(command_text)
        if text == "":
            return None, 0.0, ""

        # 数值参数类命令优先处理，例如“参数80”“参数设为50”。
        if "参数" in text and re.search(r"\d{1,3}", text):
            return "set_param", 1.0, "参数数值"

        # 第零层：语音近音容错匹配。
        # 适合“梯度”被识别成“宜都一度”等普通模糊匹配分数很低的情况。
        command, score, keyword = self.match_by_homophone_rule(text)
        if command is not None:
            return command, score, keyword

        # 第一层：关键词包含匹配。适合“请执行卡尔曼滤波”这类长句。
        for command, keywords in self.voice_command_dict.items():
            for keyword in keywords:
                keyword_clean = self.normalize_voice_text(keyword)
                if keyword_clean and (keyword_clean in text or text in keyword_clean):
                    return command, 1.0, keyword

        # 第二层：模糊匹配。适合“索贝尔提度”“罗伯斯”等识别不准的情况。
        best_command = None
        best_score = 0.0
        best_keyword = ""

        for command, keywords in self.voice_command_dict.items():
            for keyword in keywords:
                keyword_clean = self.normalize_voice_text(keyword)
                if not keyword_clean:
                    continue
                score = difflib.SequenceMatcher(None, text, keyword_clean).ratio()
                if score > best_score:
                    best_score = score
                    best_command = command
                    best_keyword = keyword

        # 0.58偏宽松，减少漏识别；如果误触发多，可改为0.65。
        if best_score >= 0.58:
            return best_command, best_score, best_keyword

        # 第三层：低分但方向明确的短口令容错。
        # 例如“宜都一度”最接近“梯度”，分数可能只有0.33，
        # 但只要当前最接近的是 Sobel 类关键词，并且文本里有“度/都/提/梯”等发音线索，
        # 就允许执行 Sobel，避免明明听到了却要求重试。
        if best_command == "sobel" and best_score >= 0.30:
            if any(ch in text for ch in ["度", "都", "梯", "提", "题", "剃", "贝", "索", "搜"]):
                return "sobel", best_score, f"{best_keyword}（低分容错）"

        if best_command == "roberts" and best_score >= 0.35:
            if any(word in text for word in ["罗", "萝", "伯", "卜", "布", "茨", "斯"]):
                return "roberts", best_score, f"{best_keyword}（低分容错）"

        if best_command == "lbp" and best_score >= 0.35:
            if any(word in text for word in ["纹", "文", "理", "二", "值", "指", "lb", "bp"]):
                return "lbp", best_score, f"{best_keyword}（低分容错）"

        return None, best_score, best_keyword

    def handle_voice_command(self, command_text: str) -> None:
        """
        根据识别到的中文语音文本执行对应功能。
        该方法在主线程运行，可以安全地调用界面函数。
        """
        text = self.normalize_voice_text(command_text)

        try:
            # 参数数值指令直接处理，不走普通匹配。
            if "参数" in text and self.handle_parameter_voice_command(text):
                feedback = f"参数已调整为{self.get_parameter_value()}"
                self.update_voice_status(f"识别到：{command_text}；执行结果：{feedback}")
                return

            command, score, keyword = self.match_voice_command(command_text)

            if command is None:
                self.update_voice_status(
                    f"识别到：{command_text}；未匹配到有效指令。最接近：{keyword}，匹配度：{score:.2f}。请用短口令重试。"
                )
                return

            feedback = self.execute_voice_command(command)
            self.update_voice_status(
                f"识别到：{command_text}；匹配：{keyword}；匹配度：{score:.2f}；执行结果：{feedback}"
            )

        except Exception as e:
            feedback = f"执行语音指令时出错：{e}"
            self.update_voice_status(f"识别到：{command_text}；{feedback}")

    def execute_voice_command(self, command: str) -> str:
        """
        根据匹配到的语音指令执行对应功能。
        """
        if command == "open_default_folder":
            self.load_default_image_folder()
            return "已打开默认文件夹"

        if command == "choose_image":
            self.choose_single_image()
            return "已打开图像选择窗口"

        if command == "choose_folder":
            self.choose_image_folder()
            return "已打开文件夹选择窗口"

        if command == "previous_image":
            self.show_previous_image()
            return "已切换到上一张"

        if command == "next_image":
            self.show_next_image()
            return "已切换到下一张"

        if command == "kalman":
            self.apply_algorithm("卡尔曼滤波")
            return "已执行卡尔曼滤波"

        if command == "roberts":
            self.apply_algorithm("Roberts算子")
            return "已执行Roberts算子"

        if command == "sobel":
            self.apply_algorithm("Sobel梯度")
            return "已执行Sobel梯度"

        if command == "lbp":
            self.apply_algorithm("LBP局部二值模式")
            return "已执行LBP局部二值模式"

        if command == "canny":
            self.apply_algorithm("Canny边缘检测")
            return "已执行Canny边缘检测"

        if command == "otsu_segment":
            self.apply_algorithm("Otsu分割+形态学轮廓")
            return "已执行Otsu分割与形态学轮廓叠加"

        if command == "pseudo_color":
            self.apply_algorithm("伪彩色增强")
            return "已执行伪彩色增强"

        if command == "glcm":
            self.open_glcm_features()
            return "已打开GLCM纹理特征"

        if command == "roi_analysis":
            self.open_roi_analysis()
            return "已打开ROI定量分析"

        if command == "quality_eval":
            self.open_quality_evaluation()
            return "已打开图像质量评价"

        if command == "batch_export":
            self.batch_process_and_export()
            return "已开始批量处理导出"

        if command == "compare":
            self.open_compare_dialog()
            return "已打开一键对比"

        if command == "histogram":
            self.open_lbp_histogram()
            return "已显示LBP直方图"

        if command == "gray_hist_compare":
            self.open_gray_histogram_compare()
            return "已打开灰度直方图对比"

        if command == "analysis_report":
            self.open_analysis_report()
            return "已生成实验分析报告"

        if command == "pipeline_sobel":
            self.apply_algorithm("卡尔曼滤波 + Sobel梯度")
            return "已执行卡尔曼加Sobel组合处理"

        if command == "pipeline_roberts":
            self.apply_algorithm("卡尔曼滤波 + Roberts算子")
            return "已执行卡尔曼加Roberts组合处理"

        if command == "pipeline_lbp":
            self.apply_algorithm("卡尔曼滤波 + LBP局部二值模式")
            return "已执行卡尔曼加LBP组合处理"

        if command == "preview":
            self.open_large_preview()
            return "已打开图像预览"

        if command == "center_roi":
            self.set_center_roi()
            return "已设置中心ROI区域"

        if command == "manual_roi":
            self.select_roi_region()
            return "已进入手动ROI选择"

        if command == "clear_roi":
            self.clear_roi_region()
            return "已清除ROI区域"

        if command == "increase_param":
            value = min(100, self.get_parameter_value() + 10)
            self.parameter_slider.setValue(value)
            return f"参数已增大为{value}"

        if command == "decrease_param":
            value = max(1, self.get_parameter_value() - 10)
            self.parameter_slider.setValue(value)
            return f"参数已减小为{value}"

        if command == "set_param":
            # 数值参数通常已在 handle_voice_command 中直接处理，这里作为备用。
            return f"参数为{self.get_parameter_value()}"

        if command == "save":
            self.save_processed_image()
            return "已打开保存窗口"

        if command == "help":
            self.show_voice_help()
            return "已打开语音指令帮助"

        if command == "stop_voice":
            self.stop_voice_control()
            return "已关闭语音控制"

        if command == "exit":
            self.close()
            return "正在退出程序"

        return "未找到可执行功能"

    def handle_parameter_voice_command(self, text: str) -> bool:
        """
        处理语音参数指令。
        支持：参数增大、参数减小、参数设为50、参数80。
        """
        current = self.get_parameter_value()

        if "增大" in text or "增加" in text or "调大" in text or "加大" in text:
            self.parameter_slider.setValue(min(100, current + 10))
            return True

        if "减小" in text or "降低" in text or "调小" in text or "减少" in text:
            self.parameter_slider.setValue(max(1, current - 10))
            return True

        match = re.search(r"(\d{1,3})", text)
        if match:
            value = int(match.group(1))
            value = int(np.clip(value, 1, 100))
            self.parameter_slider.setValue(value)
            return True

        return False

    def set_center_roi(self) -> None:
        """
        自动设置中心ROI区域，不需要鼠标框选。
        适合语音控制演示。
        """
        if not self.check_image_loaded():
            return

        h, w = self.gray_image.shape
        roi_w = int(w * 0.5)
        roi_h = int(h * 0.5)
        x = int((w - roi_w) / 2)
        y = int((h - roi_h) / 2)
        self.current_roi = (x, y, roi_w, roi_h)
        self.show_original_image()

        if self.current_algorithm_name != "未选择算法":
            self.reprocess_current_algorithm()
        else:
            self.update_info_panel()

    # -----------------------------------------------------
    # ROI区域选择
    # -----------------------------------------------------
    def select_roi_region(self) -> None:
        if not self.check_image_loaded():
            return

        image_for_roi = self.original_image.copy()
        window_name = "选择ROI区域：拖动鼠标框选后按 Enter 或 Space 确认，按 Esc 取消"
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(window_name, 900, 650)
        roi = cv2.selectROI(window_name, image_for_roi, showCrosshair=True, fromCenter=False)
        cv2.destroyWindow(window_name)

        x, y, w, h = roi
        if w <= 0 or h <= 0:
            QMessageBox.information(self, "提示", "未选择有效ROI区域。")
            return

        self.current_roi = (int(x), int(y), int(w), int(h))
        self.show_original_image()

        if self.current_algorithm_name != "未选择算法":
            self.reprocess_current_algorithm()
        else:
            self.update_info_panel()

    def clear_roi_region(self) -> None:
        self.current_roi = None
        self.show_original_image()
        if self.current_algorithm_name != "未选择算法" and self.original_image is not None:
            self.reprocess_current_algorithm()
        else:
            self.update_info_panel()

    # -----------------------------------------------------
    # 高级技术性分析功能
    # -----------------------------------------------------
    def get_active_analysis_gray(self) -> np.ndarray:
        """获取当前用于分析的灰度区域：优先ROI，否则整张图。"""
        if self.gray_image is None:
            return None
        if self.current_roi is None:
            return self.gray_image.copy()
        x, y, w, h = self.current_roi
        height, width = self.gray_image.shape
        x = max(0, min(x, width - 1))
        y = max(0, min(y, height - 1))
        w = max(1, min(w, width - x))
        h = max(1, min(h, height - y))
        return self.gray_image[y:y + h, x:x + w].copy()

    def format_metrics_text(self, title: str, metrics: dict) -> str:
        lines = [title, "=" * 34]
        for key, value in metrics.items():
            if isinstance(value, float):
                if "比例" in key or "复杂度" in key:
                    lines.append(f"{key}：{value:.4f}")
                else:
                    lines.append(f"{key}：{value:.2f}")
            else:
                lines.append(f"{key}：{value}")
        return "\n".join(lines)

    def open_roi_analysis(self) -> None:
        """ROI区域定量分析。"""
        if not self.check_image_loaded():
            return
        if self.current_roi is None:
            QMessageBox.information(self, "提示", "请先选择ROI区域，或使用语音/按钮设置中心ROI。")
            return

        x, y, w, h = self.current_roi
        roi_gray = self.get_active_analysis_gray()
        metrics = calculate_image_quality_metrics(roi_gray)
        glcm = calculate_glcm_features(roi_gray)
        metrics.update({
            "ROI左上角X": x,
            "ROI左上角Y": y,
            "ROI宽度": w,
            "ROI高度": h,
            "ROI面积/像素": int(w * h),
        })
        report = self.format_metrics_text("ROI感兴趣区域定量分析", metrics)
        report += "\n\n" + self.format_metrics_text("ROI-GLCM纹理特征", glcm)
        report += (
            "\n\n说明：ROI定量分析用于从局部区域观察医学图像灰度、清晰度、边缘比例和纹理复杂度，"
            "比整图统计更能反映某个器官、组织或病灶候选区域的局部特征。"
        )
        dialog = AnalysisReportDialog(report, default_name="roi_analysis_report.txt")
        dialog.exec_()

    def open_quality_evaluation(self) -> None:
        """图像质量评价：比较原图与当前处理结果。"""
        if not self.check_image_loaded():
            return
        original_metrics = calculate_image_quality_metrics(self.get_active_analysis_gray())
        text = self.format_metrics_text("原图/ROI图像质量评价", original_metrics)

        if self.processed_image is not None:
            processed_gray = convert_to_gray(self.processed_image) if len(self.processed_image.shape) == 3 else self.processed_image
            processed_metrics = calculate_image_quality_metrics(processed_gray)
            text += "\n\n" + self.format_metrics_text(f"处理后图像质量评价：{self.current_algorithm_name}", processed_metrics)
            text += (
                "\n\n处理前后指标解释：\n"
                "1. 卡尔曼滤波通常会降低灰度标准差和噪声波动，使图像更平滑；\n"
                "2. Sobel、Roberts、Canny会提高边缘比例或清晰度，突出组织边界；\n"
                "3. LBP和GLCM更关注纹理复杂度，适合分析组织表面纹理变化；\n"
                "4. 信息熵越高，通常说明灰度信息越丰富，但也可能包含噪声。"
            )
        else:
            text += "\n\n当前还没有处理后的图像。选择一种算法后，可对比处理前后的质量指标。"

        dialog = AnalysisReportDialog(text, default_name="quality_evaluation_report.txt")
        dialog.exec_()

    def open_glcm_features(self) -> None:
        """显示GLCM灰度共生矩阵纹理特征。"""
        if not self.check_image_loaded():
            return
        gray = self.get_active_analysis_gray()
        glcm = calculate_glcm_features(gray)
        text = self.format_metrics_text("GLCM灰度共生矩阵纹理特征", glcm)
        text += (
            "\n\n特征说明：\n"
            "1. 对比度 Contrast：反映灰度变化强弱，值越大纹理越粗糙；\n"
            "2. 同质性 Homogeneity：反映局部灰度一致性，值越大纹理越平滑；\n"
            "3. 能量 Energy：反映纹理分布集中程度；\n"
            "4. 相关性 Correlation：反映相邻像素灰度相关程度；\n"
            "5. 熵 Entropy：反映纹理信息复杂程度。\n\n"
            "LBP强调局部二值编码，GLCM强调灰度空间共现关系，两者结合可以更完整地描述医学图像纹理特征。"
        )
        dialog = AnalysisReportDialog(text, default_name="glcm_texture_features.txt")
        dialog.exec_()

    def batch_process_and_export(self) -> None:
        """批量处理当前文件夹中的图像，并导出结果图像与CSV指标表。"""
        if len(self.image_files) == 0:
            QMessageBox.information(self, "提示", "请先选择包含医学图像的文件夹。")
            return

        export_dir = QFileDialog.getExistingDirectory(self, "选择批量结果保存文件夹", "")
        if not export_dir:
            return

        algorithms = [
            ("kalman", "卡尔曼滤波"),
            ("roberts", "Roberts算子"),
            ("sobel", "Sobel梯度"),
            ("canny", "Canny边缘检测"),
            ("lbp", "LBP局部二值模式"),
            ("segment", "Otsu分割+形态学轮廓"),
            ("pseudo_color", "伪彩色增强"),
        ]
        for folder_name, _ in algorithms:
            os.makedirs(os.path.join(export_dir, folder_name), exist_ok=True)

        csv_path = os.path.join(export_dir, "analysis_report.csv")
        parameter_value = self.get_parameter_value()
        rows = []
        start_all = time.time()

        for idx, path in enumerate(self.image_files, start=1):
            self.info_label.setText(f"批量处理中：{idx}/{len(self.image_files)}  {os.path.basename(path)}")
            QApplication.processEvents()

            image, _metadata = read_medical_image(path)
            if image is None:
                rows.append({"文件名": os.path.basename(path), "状态": "读取失败"})
                continue

            gray = convert_to_gray(image)
            # 批处理时对过大图像做等比例缩放，保证运行流畅。
            max_side = 1200
            h, w = gray.shape
            if max(h, w) > max_side:
                scale = max_side / float(max(h, w))
                gray = cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

            base_name = os.path.splitext(os.path.basename(path))[0]
            base_metrics = calculate_image_quality_metrics(gray)

            for folder_name, algorithm_name in algorithms:
                try:
                    result = process_with_optional_roi(gray, None, algorithm_name, parameter_value)
                    out_path = os.path.join(export_dir, folder_name, f"{base_name}_{folder_name}.png")
                    save_image_unicode(out_path, result)
                    result_metrics = calculate_image_quality_metrics(result)
                    row = {
                        "文件名": os.path.basename(path),
                        "算法": algorithm_name,
                        "状态": "成功",
                        "原图平均灰度": base_metrics.get("平均灰度", 0),
                        "原图标准差": base_metrics.get("灰度标准差", 0),
                        "原图信息熵": base_metrics.get("信息熵", 0),
                        "处理后平均灰度": result_metrics.get("平均灰度", 0),
                        "处理后标准差": result_metrics.get("灰度标准差", 0),
                        "处理后信息熵": result_metrics.get("信息熵", 0),
                        "处理后清晰度": result_metrics.get("清晰度", 0),
                        "处理后边缘比例": result_metrics.get("边缘像素比例", 0),
                        "保存路径": out_path,
                    }
                    rows.append(row)
                except Exception as e:
                    rows.append({"文件名": os.path.basename(path), "算法": algorithm_name, "状态": f"失败：{e}"})

        if rows:
            fieldnames = sorted(set().union(*(row.keys() for row in rows)))
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)

        elapsed = time.time() - start_all
        self.update_info_panel()
        QMessageBox.information(
            self,
            "批量处理完成",
            f"已处理 {len(self.image_files)} 张图像。\n结果目录：{export_dir}\nCSV指标表：{csv_path}\n总耗时：{elapsed:.2f} 秒",
        )

    # -----------------------------------------------------
    # 一键对比
    # -----------------------------------------------------
    def open_compare_dialog(self) -> None:
        if not self.check_image_loaded():
            return

        parameter_value = self.get_parameter_value()
        start_time = time.time()
        QApplication.processEvents()

        images_dict = {
            "原图像": self.original_image,
            "卡尔曼滤波": process_with_optional_roi(self.gray_image, self.current_roi, "卡尔曼滤波", parameter_value),
            "Roberts算子": process_with_optional_roi(self.gray_image, self.current_roi, "Roberts算子", parameter_value),
            "Sobel梯度": process_with_optional_roi(self.gray_image, self.current_roi, "Sobel梯度", parameter_value),
            "LBP局部二值模式": process_with_optional_roi(self.gray_image, self.current_roi, "LBP局部二值模式", parameter_value),
        }

        self.last_process_time = time.time() - start_time
        self.update_info_panel()

        dialog = CompareDialog(images_dict)
        dialog.exec_()

    # -----------------------------------------------------
    # LBP直方图
    # -----------------------------------------------------
    def open_lbp_histogram(self) -> None:
        if not self.check_image_loaded():
            return

        parameter_value = self.get_parameter_value()
        lbp_img = process_with_optional_roi(
            self.gray_image,
            self.current_roi,
            "LBP局部二值模式",
            parameter_value,
        )

        dialog = LbpHistogramDialog(lbp_img)
        dialog.exec_()

    # -----------------------------------------------------
    # 灰度直方图对比与实验分析报告
    # -----------------------------------------------------
    def open_gray_histogram_compare(self) -> None:
        """
        显示处理前后灰度直方图对比。
        """
        if not self.check_image_loaded():
            return

        if self.processed_image is None:
            QMessageBox.information(self, "提示", "请先选择一种算法，生成处理后的图像，再进行灰度直方图对比。")
            return

        dialog = GrayHistogramCompareDialog(self.gray_image, self.processed_image, self.current_algorithm_name)
        dialog.exec_()

    @staticmethod
    def calculate_entropy(gray_image: np.ndarray) -> float:
        """
        计算图像信息熵。熵值越大，说明灰度分布越丰富。
        """
        gray = gray_image.astype(np.uint8)
        hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).flatten()
        prob = hist / max(np.sum(hist), 1)
        prob = prob[prob > 0]
        entropy = -np.sum(prob * np.log2(prob))
        return float(entropy)

    @staticmethod
    def calculate_sharpness(gray_image: np.ndarray) -> float:
        """
        使用拉普拉斯方差衡量图像清晰度。
        """
        gray = gray_image.astype(np.uint8)
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())

    @staticmethod
    def get_basic_statistics(gray_image: np.ndarray) -> dict:
        gray = gray_image.astype(np.float32)
        return {
            "mean": float(np.mean(gray)),
            "std": float(np.std(gray)),
            "min": int(np.min(gray)),
            "max": int(np.max(gray)),
        }

    def build_analysis_report_text(self) -> str:
        """
        根据当前图像和处理结果生成实验分析报告文本。
        """
        original_gray = self.gray_image
        processed_gray = convert_to_gray(self.processed_image) if len(self.processed_image.shape) == 3 else self.processed_image

        original_stats = self.get_basic_statistics(original_gray)
        processed_stats = self.get_basic_statistics(processed_gray)
        original_entropy = self.calculate_entropy(original_gray)
        processed_entropy = self.calculate_entropy(processed_gray)
        original_sharpness = self.calculate_sharpness(original_gray)
        processed_sharpness = self.calculate_sharpness(processed_gray)

        edge_ratio = float(np.sum(processed_gray > 0) / processed_gray.size * 100.0)
        image_name = os.path.basename(self.current_image_path) if self.current_image_path else "未命名图像"
        h, w = original_gray.shape
        parameter_value = self.get_parameter_value()

        if self.current_roi is None:
            roi_text = "未启用ROI，处理区域为整张图像"
        else:
            x, y, rw, rh = self.current_roi
            roi_text = f"已启用ROI，区域位置：x={x}, y={y}, w={rw}, h={rh}"

        algorithm_explanation = {
            "卡尔曼滤波": "优化版卡尔曼滤波采用前向与反向递推估计，并结合边缘保持融合策略，主要用于降低随机噪声和局部灰度波动，同时尽量保留医学结构轮廓。",
            "Roberts算子": "优化版Roberts算子先进行轻度去噪和局部对比度增强，再用2×2交叉梯度模板提取细小、斜向和局部灰度突变边缘，突出快速、细边缘检测特点。",
            "Sobel梯度": "优化版Sobel梯度先增强局部组织对比度，再计算水平和垂直方向梯度，并通过自适应阈值和轻微闭运算提高主轮廓边缘的连续性。",
            "LBP局部二值模式": "优化版LBP在标准8邻域编码基础上融合局部标准差信息，能够更突出组织纹理、粗糙度和局部灰度模式差异。",
            "卡尔曼滤波 + Sobel梯度": "组合流程先使用卡尔曼滤波进行去噪，再使用Sobel梯度提取边缘，可减少噪声对边缘检测的干扰。",
            "卡尔曼滤波 + Roberts算子": "组合流程先使用卡尔曼滤波平滑图像，再使用Roberts算子提取细小边缘，使边缘结果更干净。",
            "卡尔曼滤波 + LBP局部二值模式": "组合流程先使用卡尔曼滤波减少噪声，再使用LBP提取纹理特征，有助于提高纹理分析的稳定性。",
            "Canny边缘检测": "Canny边缘检测结合高斯平滑、非极大值抑制和双阈值连接策略，能够得到较细且连续的边缘结果，适合与Roberts和Sobel进行对比。",
            "Otsu分割+形态学轮廓": "Otsu自动阈值分割无需人工设定阈值，结合形态学开闭运算可以去除小噪声并填补局部空洞，轮廓叠加便于观察分割区域边界。",
            "伪彩色增强": "伪彩色增强将医学灰度图像映射到彩色空间，能够强化灰度层次差异，使CT、MRI和超声图像中的结构差别更直观。",
        }.get(self.current_algorithm_name, "当前算法用于医学图像处理结果展示与分析。")

        conclusion = []
        if "Sobel" in self.current_algorithm_name or "Roberts" in self.current_algorithm_name:
            conclusion.append(f"处理后边缘像素比例约为 {edge_ratio:.2f}%，可用于观察图像中灰度突变区域和组织轮廓。")
        if "卡尔曼" in self.current_algorithm_name:
            conclusion.append("卡尔曼滤波或组合流程能够在一定程度上平滑图像灰度变化，减少噪声对后续处理的影响。")
        if "LBP" in self.current_algorithm_name:
            texture_complexity = processed_stats["std"] / 255.0
            conclusion.append(f"LBP纹理复杂度约为 {texture_complexity:.4f}，该值可作为图像局部纹理变化强弱的参考。")
        if "Canny" in self.current_algorithm_name:
            conclusion.append("Canny结果通常边缘更细且连续，适合与Roberts的细小边缘和Sobel的主轮廓边缘进行对比。")
        if "Otsu" in self.current_algorithm_name:
            conclusion.append("Otsu分割结果可用于观察图像中明显灰度区域的自动分割效果，形态学处理提升了区域连贯性。")
        if "伪彩色" in self.current_algorithm_name:
            conclusion.append("伪彩色增强将灰度差异转化为色彩差异，适合在演示中突出组织灰度层次变化。")
        if not conclusion:
            conclusion.append("该处理结果可以作为医学图像增强、边缘检测或纹理分析的实验依据。")

        report = (
            "医学图像处理实验分析报告\n"
            + ("=" * 34) + "\n\n"
            f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"当前图像：{image_name}\n"
            f"图像尺寸：{w} × {h}\n"
            f"当前算法：{self.current_algorithm_name}\n"
            f"参数值：{parameter_value}\n"
            f"ROI状态：{roi_text}\n"
            f"处理时间：{self.last_process_time:.3f} 秒\n"
            f"保存状态：{self.save_status}\n"
            f"DICOM状态：{('已启用，窗宽=' + str(int(self.current_window_width or 0)) + '，窗位=' + str(int(self.current_window_center or 0))) if self.dicom_metadata.get('is_dicom') else '普通图像'}\n\n"
            "一、算法作用说明\n"
            f"{algorithm_explanation}\n\n"
            "二、原图像统计指标\n"
            f"平均灰度：{original_stats['mean']:.2f}\n"
            f"灰度标准差：{original_stats['std']:.2f}\n"
            f"最小灰度值：{original_stats['min']}\n"
            f"最大灰度值：{original_stats['max']}\n"
            f"信息熵：{original_entropy:.4f}\n"
            f"清晰度（拉普拉斯方差）：{original_sharpness:.4f}\n\n"
            "三、处理后图像统计指标\n"
            f"平均灰度：{processed_stats['mean']:.2f}\n"
            f"灰度标准差：{processed_stats['std']:.2f}\n"
            f"最小灰度值：{processed_stats['min']}\n"
            f"最大灰度值：{processed_stats['max']}\n"
            f"信息熵：{processed_entropy:.4f}\n"
            f"清晰度（拉普拉斯方差）：{processed_sharpness:.4f}\n"
            f"非零像素比例 / 边缘像素比例：{edge_ratio:.2f}%\n\n"
            "四、结果分析\n"
            + "\n".join([f"{i + 1}. {item}" for i, item in enumerate(conclusion)])
            + "\n\n"
            "五、报告说明\n"
            "本报告由系统根据当前医学图像、当前算法处理结果和统计指标自动生成，"
            "统计指标仅用于算法效果对比与演示，"
            "不作为医学诊断依据。\n"
        )
        return report

    def open_analysis_report(self) -> None:
        """
        一键生成实验分析报告，并支持保存为txt文件。
        """
        if not self.check_image_loaded():
            return

        if self.processed_image is None:
            QMessageBox.information(self, "提示", "请先选择一种算法，生成处理后的图像，再生成实验分析报告。")
            return

        report_text = self.build_analysis_report_text()
        image_name = os.path.basename(self.current_image_path) if self.current_image_path else "image"
        name, _ = os.path.splitext(image_name)
        default_name = f"{name}_{self.current_algorithm_name}_实验分析报告.txt"
        dialog = AnalysisReportDialog(report_text, default_name)
        dialog.exec_()

    # -----------------------------------------------------
    # 打开与保存
    # -----------------------------------------------------
    def open_large_preview(self) -> None:
        if not self.check_image_loaded():
            return

        if self.processed_image is None:
            QMessageBox.information(self, "提示", "请先点击一种算法，生成处理后的图像。")
            return

        dialog = ImagePreviewDialog(self.display_original_image, self.processed_image, self.current_algorithm_name)
        dialog.exec_()

    def save_processed_image(self) -> None:
        if self.processed_image is None:
            QMessageBox.information(self, "提示", "当前没有处理后的图像，请先选择算法进行处理。")
            return

        if self.current_image_path:
            base_name = os.path.basename(self.current_image_path)
            name, _ = os.path.splitext(base_name)
            default_name = f"{name}_{self.current_algorithm_name}.png"
        else:
            default_name = "processed_image.png"

        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "保存处理后的图像",
            default_name,
            "PNG图像 (*.png);;JPG图像 (*.jpg);;BMP图像 (*.bmp);;TIFF图像 (*.tif)",
        )

        if not save_path:
            return

        success = save_image_unicode(save_path, self.processed_image)
        if success:
            self.save_status = f"已保存：{save_path}"
            self.update_info_panel()
            QMessageBox.information(self, "保存成功", f"处理后的图像已保存到：\n{save_path}")
        else:
            QMessageBox.warning(self, "保存失败", "图像保存失败，请检查保存路径或文件格式。")

    # -----------------------------------------------------
    # 信息栏
    # -----------------------------------------------------
    def update_info_panel(self) -> None:
        if self.original_image is None:
            text = "状态：等待选择医学图像。"
            self.info_label.setText(text)
            return

        h, w = self.gray_image.shape
        image_name = os.path.basename(self.current_image_path) if self.current_image_path else "未命名图像"
        parameter_value = self.get_parameter_value()

        if self.current_roi is None:
            roi_text = "未启用"
        else:
            x, y, rw, rh = self.current_roi
            roi_text = f"已启用，x={x}, y={y}, w={rw}, h={rh}"

        dicom_text = "普通图像"
        if self.dicom_metadata.get("is_dicom"):
            dicom_text = (
                f"DICOM({self.dicom_metadata.get('modality', '未知')})，"
                f"窗宽={int(self.current_window_width or 0)}，窗位={int(self.current_window_center or 0)}"
            )

        text = (
            f"当前图像：{image_name}    "
            f"图像类型：{dicom_text}    "
            f"图像尺寸：{w} × {h}    "
            f"当前算法：{self.current_algorithm_name}    "
            f"参数值：{parameter_value}    "
            f"ROI状态：{roi_text}    "
            f"处理时间：{self.last_process_time:.3f} 秒    "
            f"保存状态：{self.save_status}"
        )
        self.info_label.setText(text)

    # -----------------------------------------------------
    # 主窗口居中显示
    # -----------------------------------------------------
    def center_window(self) -> None:
        """
        将主窗口移动到当前屏幕的可用区域中央。

        说明：
        1. 使用 availableGeometry()，避免窗口被任务栏遮挡。
        2. 在 __init__ 中调用一次，保证程序启动后不会出现在屏幕下方。
        3. main() 中 show() 后再延迟调用一次，确保窗口装饰栏计算完成后仍保持居中。
        """
        screen = QApplication.primaryScreen()
        if screen is None:
            return

        screen_geometry = screen.availableGeometry()
        window_geometry = self.frameGeometry()
        window_geometry.moveCenter(screen_geometry.center())
        self.move(window_geometry.topLeft())

    # -----------------------------------------------------
    # 关闭窗口时停止语音线程
    # -----------------------------------------------------
    def closeEvent(self, event) -> None:
        if self.voice_worker is not None:
            self.voice_worker.stop()
        super().closeEvent(event)

    # -----------------------------------------------------
    # 固定图像框后，不再在 resizeEvent 中反复刷新图片
    # 这样可以避免布局在重绘过程中不断变化
    # -----------------------------------------------------
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)


# =========================================================
# 十一、程序入口
# =========================================================


def main() -> None:
    app = QApplication(sys.argv)
    window = MedicalImageProcessingSystem()
    window.show()
    # 窗口显示后再居中一次，确保标题栏和边框尺寸计算完成。
    QTimer.singleShot(0, window.center_window)
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
