铅当量测量分析系统 (Lead Equivalent Measurement)
本项目是一个基于 DICOM 医学影像的交互式分析工具，用于通过灰度标定曲线计算待测区域的铅当量（mmPb），并生成包含诊断结论的 PDF 报告。适用于 X 射线防护材料、辐射屏蔽性能评估等场景。

主要功能：
加载 DICOM 序列 – 自动读取文件夹内所有 .dcm 文件，支持多帧图像平均处理。
标定曲线拟合 – 手动框选 6 个标准厚度标定块（0.15、0.20、0.40、0.45、0.50、0.75 mmPb），程序自动按灰度排序并拟合指数衰减曲线 G = a·exp(-b·T) + c。
铅当量测量 – 框选待测区域，基于拟合曲线换算铅当量，支持输入标称值并计算相对偏差。
数据记录 – 自动生成 CSV 文件，记录每个 ROI 的坐标、灰度值、铅当量、偏差等信息。
可视化预览 – 为每个 ROI 保存独立预览图（全图定位 + 局部放大）。
一键导出报告 – 生成专业 PDF 报告，包含标定曲线、测量结论、不确定度估算及屏蔽性能评级。

环境要求：
Python 3.8+
依赖库：pydicom, matplotlib, numpy, scipy, tkinter（通常随 Python 自带）

安装依赖：pip install pydicom matplotlib numpy scipy

运行程序：python app.py




web_app/
├── app.py               # Flask 后端
├── templates/
│   └── index.html       # 主页面
└── static/
    ├── css/
    │   └── style.css    # 样式
    └── js/
        └── main.js      # 前端交互
