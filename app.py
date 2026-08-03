import matplotlib
matplotlib.use('Agg')
from flask import Flask, request, jsonify, send_file, render_template
import os
import tempfile
from datetime import datetime
import zipfile
import numpy as np
from pathlib import Path
from io import BytesIO
import base64
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

# 导入您原有的函数（需重构为模块）
from lead_core import (
    load_dicom_image,
    compute_multi_image_roi_stats,
    compute_multi_image_roi_stats_with_variance,
    fit_calibration_curve,
    estimate_lead_equivalent,
    STANDARD_THICKNESSES,
    exponential_model,
    normalize_for_preview,
    # ... 其他
)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 允许500MB上传

# 全局存储当前会话数据（简单起见，实际应使用session或redis）
session_data = {
    'images': [],           # 原始图像列表 (np.array)
    'filenames': [],
    'calibration_rois': [], # 已框选的ROI统计
    'fit_params': None,
    'fit_stats': None,
    'measurement_detail': None,
    'mode': 'calibration',  # 'calibration' or 'measurement'
    'current_index': 0,     # 标定进度
}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_files():
    """接收上传的DICOM文件（支持zip或直接多文件）"""
    files = request.files.getlist('dicom_files')
    if not files:
        return jsonify({'error': '没有上传文件'}), 400

    # 保存到临时目录并按文件名排序
    tmp_dir = tempfile.mkdtemp()
    paths = []
    for f in files:
        if f.filename.endswith('.zip'):
            # 解压zip
            zip_path = os.path.join(tmp_dir, f.filename)
            f.save(zip_path)
            with zipfile.ZipFile(zip_path, 'r') as z:
                z.extractall(tmp_dir)
        else:
            path = os.path.join(tmp_dir, f.filename)
            f.save(path)
            paths.append(path)

    # 收集所有.dcm文件
    all_dcm = sorted(Path(tmp_dir).glob('*.dcm'))
    if not all_dcm:
        return jsonify({'error': '未找到任何.dcm文件'}), 400

    # 加载图像
    images = [load_dicom_image(p) for p in all_dcm]
    # 检查尺寸一致性
    shapes = [img.shape for img in images]
    if not all(s == shapes[0] for s in shapes):
        return jsonify({'error': '所有DICOM尺寸不一致'}), 400

    # 存储到会话（这里简单存全局，实际应用应存session）
    session_data['images'] = images
    session_data['filenames'] = [p.name for p in all_dcm]
    session_data['calibration_rois'] = []
    session_data['fit_params'] = None
    session_data['fit_stats'] = None
    session_data['measurement_detail'] = None
    session_data['mode'] = 'calibration'
    session_data['current_index'] = 0

    # 生成第一张图的预览Base64
    img_preview = normalize_for_preview(images[0])
    img_bytes = BytesIO()
    plt.imsave(img_bytes, img_preview, cmap='gray', format='png')
    img_base64 = base64.b64encode(img_bytes.getvalue()).decode('utf-8')

    return jsonify({
        'status': 'ok',
        'message': f'成功加载 {len(images)} 张图像',
        'image_data': img_base64,
        'num_images': len(images),
        'height': images[0].shape[0],
        'width': images[0].shape[1],
        'thicknesses': STANDARD_THICKNESSES.tolist(),
        'next_thickness': STANDARD_THICKNESSES[0]
    })

@app.route('/add_calibration_roi', methods=['POST'])
def add_calibration_roi():
    """接收标定ROI坐标，计算多图统计"""
    data = request.json
    x_min, y_min, x_max, y_max = data['x_min'], data['y_min'], data['x_max'], data['y_max']
    images = session_data['images']
    try:
        roi_stats = compute_multi_image_roi_stats(images, x_min, y_min, x_max, y_max)
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    # 添加到列表
    session_data['calibration_rois'].append(roi_stats)
    current_idx = len(session_data['calibration_rois']) - 1
    thickness = STANDARD_THICKNESSES[current_idx]

    # 判断是否完成标定
    if len(session_data['calibration_rois']) == len(STANDARD_THICKNESSES):
        # 执行拟合
        gray_vals = np.array([r.robust_mean for r in session_data['calibration_rois']])
        params, stats = fit_calibration_curve(STANDARD_THICKNESSES, gray_vals)
        session_data['fit_params'] = params
        session_data['fit_stats'] = stats
        session_data['mode'] = 'measurement'
        # 生成曲线数据用于前端绘图
        t_smooth = np.linspace(0, max(STANDARD_THICKNESSES)*1.15, 100)
        g_smooth = exponential_model(t_smooth, *params)
        curve_data = {
            't': t_smooth.tolist(),
            'g': g_smooth.tolist(),
            'points_t': STANDARD_THICKNESSES.tolist(),
            'points_g': gray_vals.tolist(),
            'params': params.tolist(),
            'stats': stats
        }
        return jsonify({
            'status': 'calibration_done',
            'message': '标定完成，进入测量模式',
            'curve_data': curve_data,
            'next_mode': 'measurement'
        })

    # 未完成，返回下一个厚度
    next_thick = STANDARD_THICKNESSES[len(session_data['calibration_rois'])]
    return jsonify({
        'status': 'ok',
        'message': f'标定块 {current_idx+1} 已记录',
        'next_thickness': next_thick,
        'roi_stats': {
            'raw_mean': roi_stats.raw_mean,
            'robust_mean': roi_stats.robust_mean,
            'valid_ratio': roi_stats.valid_ratio
        }
    })

@app.route('/measure', methods=['POST'])
def measure_roi():
    if session_data['fit_params'] is None:
        return jsonify({'error': '尚未完成标定'}), 400

    data = request.json
    x_min, y_min, x_max, y_max = data['x_min'], data['y_min'], data['x_max'], data['y_max']
    images = session_data['images']
    try:
        roi_stats, std_across = compute_multi_image_roi_stats_with_variance(images, x_min, y_min, x_max, y_max)
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    params = session_data['fit_params']
    try:
        thickness, warnings, is_extrapolated = estimate_lead_equivalent(
            roi_stats.robust_mean, params,
            (STANDARD_THICKNESSES.min(), STANDARD_THICKNESSES.max())
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    # ---- 计算不确定度 ----
    a, b, c = params
    slope_abs = a * b * np.exp(-b * thickness) if thickness > 0 else 1e-9
    gray_uncertainty = std_across + session_data['fit_stats']['rmse']
    lead_uncertainty = gray_uncertainty / slope_abs if slope_abs > 1e-9 else 0
    expanded_uncertainty = 2 * lead_uncertainty

    # ---- 评级（必须在这里定义） ----
    if thickness >= 0.75:
        grade = '★★★★★ (极佳)'
    elif thickness >= 0.50:
        grade = '★★★★☆ (优秀)'
    elif thickness >= 0.25:
        grade = '★★★☆☆ (良好)'
    else:
        grade = '★★☆☆☆ (较低)'

    # ---- 存储测量结果（包含矩形坐标供报告使用） ----
    session_data['measurement_detail'] = (
        roi_stats.robust_mean,
        thickness,
        std_across,
        is_extrapolated,
        expanded_uncertainty,
        grade
    )
    session_data['measurement_rect'] = {
        'x_min': x_min,
        'y_min': y_min,
        'x_max': x_max,
        'y_max': y_max
    }

    # ---- 返回 ----
    return jsonify({
        'status': 'ok',
        'lead_equivalent': thickness,
        'uncertainty': expanded_uncertainty,
        'is_extrapolated': bool(is_extrapolated),
        'grade': grade,
        'robust_mean': roi_stats.robust_mean,      # 直接从 roi_stats 取
        'std_across_images': std_across,
        'valid_ratio': roi_stats.valid_ratio,      # 如存在该属性
        'warnings': warnings
    })

@app.route('/export_pdf', methods=['GET'])
def export_pdf():
    """生成并返回PDF报告"""
    # 检查标定是否完成
    if session_data.get('fit_params') is None:
        return jsonify({'error': '标定尚未完成，无法导出报告'}), 400

    images = session_data.get('images', [])
    if not images:
        return jsonify({'error': '没有加载图像'}), 400

    calibration_rois = session_data.get('calibration_rois', [])
    fit_params = session_data['fit_params']
    fit_stats = session_data.get('fit_stats', {})
    measurement_detail = session_data.get('measurement_detail', None)
    measurement_rect = session_data.get('measurement_rect', None)
    dicom_filename = session_data.get('filenames', ['unknown'])[0] if session_data.get('filenames') else 'unknown'

    first_img = images[0]
    display_min, display_max = np.percentile(first_img, [1, 99])

    # 创建内存中的 PDF
    pdf_bytes = BytesIO()
    with PdfPages(pdf_bytes) as pdf:
        fig_report = plt.figure(figsize=(11.69, 8.27), dpi=150)

        # ---- 左侧：DICOM 图像 ----
        ax_img = fig_report.add_axes([0.05, 0.15, 0.40, 0.70])
        ax_img.imshow(first_img, cmap='gray', vmin=display_min, vmax=display_max)
        # 绘制标定框
        for i, roi in enumerate(calibration_rois):
            rect = plt.Rectangle(
                (roi.x_min, roi.y_min),
                roi.x_max - roi.x_min,
                roi.y_max - roi.y_min,
                fill=False,
                edgecolor='tab:orange',
                linewidth=1.5,
            )
            ax_img.add_patch(rect)
            thickness = STANDARD_THICKNESSES[i]
            ax_img.text(
                roi.x_min,
                roi.y_min - 8,
                f"{thickness:.3f}mm",
                color='tab:orange',
                fontsize=8,
                bbox={"facecolor": "white", "alpha": 0.6, "edgecolor": "none"},
            )
        # 如果有测量框，绘制绿色测量框
        if measurement_rect is not None:
            rect_meas = plt.Rectangle(
                (measurement_rect['x_min'], measurement_rect['y_min']),
                measurement_rect['x_max'] - measurement_rect['x_min'],
                measurement_rect['y_max'] - measurement_rect['y_min'],
                fill=False,
                edgecolor='lime',
                linewidth=2,
            )
            ax_img.add_patch(rect_meas)
            ax_img.text(
                measurement_rect['x_min'],
                measurement_rect['y_min'] - 8,
                "测量区域",
                color='lime',
                fontsize=8,
                bbox={"facecolor": "white", "alpha": 0.6, "edgecolor": "none"},
            )
        ax_img.set_title(f"原始图像 (共 {len(images)} 张平均)")
        ax_img.axis("off")

        # ---- 右侧上方：拟合曲线 ----
        ax_curve = fig_report.add_axes([0.55, 0.50, 0.40, 0.35])
        gray_values = np.array([r.robust_mean for r in calibration_rois], dtype=np.float64)
        thicknesses = STANDARD_THICKNESSES[:len(calibration_rois)]
        ax_curve.scatter(thicknesses, gray_values, color='tab:red', s=80, label='标定点')
        if fit_params is not None:
            t_smooth = np.linspace(0.0, max(STANDARD_THICKNESSES) * 1.15, 200)
            g_smooth = exponential_model(t_smooth, *fit_params)
            ax_curve.plot(t_smooth, g_smooth, color='tab:blue', linewidth=2.5, label='拟合曲线')
        ax_curve.set_xlabel('铅当量 T (mmPb)', fontsize=10)
        ax_curve.set_ylabel('灰度均值 G', fontsize=10)
        ax_curve.grid(True, linestyle='--', alpha=0.4)
        ax_curve.legend(loc='best')

        # ---- 右侧下方：结论与评估 ----
        info_text_x = 0.55
        info_text_y = 0.08

        a, b, c = fit_params
        stats = fit_stats or {}
        rmse = stats.get('rmse', 0.0)
        r2 = stats.get('r2', 0.0)

        conclusion_lines = [
            "【标定参数】",
            f"拟合公式: G = {a:.3f}·exp(-{b:.5f}·T) + {c:.3f}",
            f"决定系数 R² = {r2:.6f}  (拟合优度{'极佳' if r2 > 0.995 else '良好' if r2 > 0.98 else '一般'})",
            f"残差 RMSE = {rmse:.4f}  (模型预测精度)",
            "",
        ]

        if measurement_detail is not None:
            gray_val, lead_eq, std_img, is_extrapolated, expanded_uncertainty, grade = measurement_detail
            conclusion_lines.append("【测量结论】")
            conclusion_lines.append(f"被测区域等效铅当量: {lead_eq:.5f} ± {expanded_uncertainty:.5f} mmPb (扩展不确定度, k=2)")
            if is_extrapolated:
                conclusion_lines.append("⚠️ 警告: 该结果位于标定范围之外，属于外推计算，精度可能下降。")
            else:
                conclusion_lines.append("✓ 结果在有效标定范围内，数据可靠。")

            if std_img < 1.0:
                stability = "优秀 (序列间灰度波动极小)"
            elif std_img < 3.0:
                stability = "良好"
            else:
                stability = "一般 (建议检查图像配准或噪声)"
            conclusion_lines.append(f"多图数据稳定性: {stability} (灰度标准差 {std_img:.3f})")

            conclusion_lines.append("")
            conclusion_lines.append("【屏蔽性能评级】")
            conclusion_lines.append(f"评级: {grade}")
        else:
            conclusion_lines.append("【测量结论】")
            conclusion_lines.append("尚未进行测量框选，请框选待测区域以获取铅当量结论。")

        fig_report.text(
            info_text_x,
            info_text_y,
            "\n".join(conclusion_lines),
            fontsize=9.5,
            va="bottom",
            bbox={"facecolor": "#eef6ff", "alpha": 0.95, "edgecolor": "#2c3e50",
                  "boxstyle": "round,pad=0.6", "linewidth": 1.5},
        )

        # ---- 报告标题 ----
        fig_report.text(
            0.5,
            0.96,
            "铅当量测量分析报告 (含诊断结论)",
            ha="center",
            fontsize=18,
            fontweight="bold",
        )
        
        # ---- 修复：使用已导入的 datetime ----
        fig_report.text(
            0.5,
            0.92,
            f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  文件: {dicom_filename}",
            ha="center",
            fontsize=10,
            color="gray",
        )

        pdf.savefig(fig_report)
        plt.close(fig_report)

    pdf_bytes.seek(0)
    return send_file(
        pdf_bytes,
        as_attachment=True,
        download_name='report.pdf',
        mimetype='application/pdf'
    )

@app.route('/reset', methods=['POST'])
def reset_calibration():
    """重置标定"""
    images = session_data.get('images', [])
    if not images:
        return jsonify({'error': '没有加载图像'}), 400
    
    session_data['calibration_rois'] = []
    session_data['fit_params'] = None
    session_data['fit_stats'] = None
    session_data['measurement_detail'] = None
    session_data['measurement_rect'] = None
    session_data['mode'] = 'calibration'
    session_data['current_index'] = 0
    
    return jsonify({
        'status': 'ok',
        'message': '已重置标定',
        'next_thickness': STANDARD_THICKNESSES[0]
    })

@app.route('/get_status', methods=['GET'])
def get_status():
    """获取当前状态"""
    return jsonify({
        'mode': session_data.get('mode', 'calibration'),
        'calibration_count': len(session_data.get('calibration_rois', [])),
        'total_calibration': len(STANDARD_THICKNESSES),
        'has_fit': session_data.get('fit_params') is not None,
        'has_measurement': session_data.get('measurement_detail') is not None,
    })

if __name__ == '__main__':
    app.run(debug=True)