# 医学图像处理综合应用系统

[English](README.md) | **简体中文**

![Python](https://img.shields.io/badge/python-3.9%2B-3776AB?logo=python&logoColor=white)
![PyQt5](https://img.shields.io/badge/PyQt5-5.15-41CD52?logo=qt&logoColor=white)
![OpenCV](https://img.shields.io/badge/OpenCV-4.x-5C3EE8?logo=opencv&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)

基于 PyQt5 + OpenCV 的桌面端医学影像处理与分析工具。支持普通图像和 DICOM 影像（含窗宽窗位调节），集成增强、边缘检测、纹理分析、分割等经典算法，参数滑块实时刷新结果；可对整图或 ROI 区域做定量分析，并支持通过科大讯飞语音 API 用中文口令免鼠标操作。

![主界面：Otsu 分割与轮廓叠加](docs/screenshots/main_otsu.png)

## 功能

| 模块 | 说明 |
|---|---|
| **图像读取** | 单张图像、文件夹浏览（上一张 / 下一张）、**DICOM**（`.dcm`）读取与窗宽窗位滑块、支持中文路径 |
| **去噪** | 二维**卡尔曼滤波**：行、列方向分别做前向 + 反向一维卡尔曼滤波，与原图融合后再做轻度双边滤波 |
| **边缘检测** | **Roberts**、**Sobel**、**Canny**，基于百分位的自适应阈值 |
| **纹理分析** | 8 邻域 **LBP** 及其直方图与统计量；NumPy 手写 **GLCM** 特征（对比度、同质性、能量、相关性、熵） |
| **分割** | **Otsu** 阈值 → 形态学开 / 闭运算 → 轮廓叠加 |
| **增强** | **伪彩色**映射；边缘与纹理算子前置 CLAHE 增强 |
| **组合流程** | 卡尔曼 + Sobel / Roberts / LBP |
| **ROI** | 框选区域仅处理该区域；定量报告包括灰度统计、拉普拉斯清晰度、信息熵、边缘比例、GLCM |
| **评价** | 处理前后灰度直方图对比、图像质量评价、一键生成分析报告（可导出 `.txt`） |
| **批量处理** | 对文件夹内所有图像运行全部算法，导出结果图与 **CSV** 指标表 |
| **语音控制**（可选） | 讯飞流式语音听写（WebSocket），中文口令模糊匹配，覆盖所有按钮功能 |

![算法效果一览](docs/screenshots/algorithm_gallery.jpg)

## 快速开始

```bash
pip install -r requirements.txt
python medical_image_system.py
```

[`images/`](images/) 中附带一张合成的 Shepp-Logan 体模图，可直接体验所有算法：点击 **选择原图像** 选中图片，再点击任意算法按钮即可。

### 语音控制（可选）

在[讯飞开放平台](https://www.xfyun.cn/)创建应用并开通“语音听写”，将凭据设置为环境变量：

```bat
setx XFYUN_APPID "你的APPID"
setx XFYUN_API_KEY "你的APIKey"
setx XFYUN_API_SECRET "你的APISecret"
```

重启终端后点击 **开启语音控制**，可以说“打开默认文件夹”（加载 `images/`）、“Sobel”、“下一张”、“一键对比”、“帮助”等。凭据只从环境变量读取，代码中不保存任何密钥。

![伪彩色增强](docs/screenshots/main_pseudocolor.png)

## 代码结构

`medical_image_system.py` 为单文件自包含模块：

| 部分 | 内容 |
|---|---|
| 读写工具 | 中文路径安全读写、DICOM 解码与窗宽窗位、文件夹扫描 |
| 算法 | `kalman_filter_image`、`roberts_operator`、`sobel_gradient`、`canny_edge_detection`、`lbp_local_binary_pattern`、`otsu_morphology_contour`、`pseudo_color_enhancement` |
| 指标 | 信息熵、拉普拉斯方差清晰度、边缘比例、质量评价、`calculate_glcm_features` |
| 对话框 | 放大查看、一键对比、LBP 直方图、灰度直方图对比、分析报告 |
| `XfyunVoiceRecognitionWorker` | 后台线程：HMAC-SHA256 签名鉴权、麦克风音频流式上传，通过 Qt 信号返回识别文本 |
| `MedicalImageProcessingSystem` | 主窗口、ROI 选择、防抖实时重处理、语音指令路由 |

## 说明

* 附带图像为计算机合成体模，不包含任何患者数据；使用真实 DICOM 前请先脱敏。
* 所有指标仅用于算法效果对比，**不作为医学诊断依据**。

## 许可证

[MIT](LICENSE)
