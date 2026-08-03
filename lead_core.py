from __future__ import annotations
from dataclasses import dataclass


import argparse
import csv
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Rectangle
from matplotlib.widgets import Button, RectangleSelector
from scipy.optimize import curve_fit
from matplotlib.backends.backend_pdf import PdfPages


def import_external_pydicom():
    """Import the third-party pydicom package even if the project has a local pydicom.py."""
    project_dir = Path(__file__).resolve().parent
    original_sys_path = sys.path[:]
    try:
        filtered = []
        for entry in original_sys_path:
            candidate = Path(entry or os.curdir).resolve()
            if candidate == project_dir:
                continue
            filtered.append(entry)
        sys.path[:] = filtered
        import pydicom as external_pydicom
        return external_pydicom
    finally:
        sys.path[:] = original_sys_path


pydicom = import_external_pydicom()

# 6个标定厚度
STANDARD_THICKNESSES = np.array([0.15, 0.20, 0.40, 0.45, 0.50, 0.75], dtype=np.float64)

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


@dataclass
class RoiStats:
    x_min: int
    y_min: int
    x_max: int
    y_max: int
    raw_mean: float
    robust_mean: float
    valid_ratio: float


def load_dicom_image(file_path: Path) -> np.ndarray:
    ds = pydicom.dcmread(str(file_path))
    raw = ds.pixel_array.astype(np.float32)
    slope = float(getattr(ds, "RescaleSlope", 1.0))
    intercept = float(getattr(ds, "RescaleIntercept", 0.0))
    image = raw * slope + intercept

    if getattr(ds, "PhotometricInterpretation", "") == "MONOCHROME1":
        image = np.max(image) - image

    return image


def compute_iqr_robust_mean(roi_data: np.ndarray) -> tuple[float, float, float]:
    if roi_data.size == 0:
        raise ValueError("ROI 为空，请重新框选。")

    q1 = float(np.percentile(roi_data, 25))
    q3 = float(np.percentile(roi_data, 75))
    iqr = q3 - q1
    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr
    valid = roi_data[(roi_data >= lower) & (roi_data <= upper)]

    raw_mean = float(np.mean(roi_data))
    if valid.size == 0:
        return raw_mean, raw_mean, 0.0

    robust_mean = float(np.mean(valid))
    valid_ratio = float(valid.size / roi_data.size)
    return raw_mean, robust_mean, valid_ratio


def compute_multi_image_roi_stats(images: list[np.ndarray], x_min: int, y_min: int, x_max: int, y_max: int) -> RoiStats:
    all_pixels = []
    for img in images:
        roi = img[y_min:y_max, x_min:x_max]
        if roi.size == 0:
            continue
        all_pixels.append(roi.ravel())
    if not all_pixels:
        raise ValueError("所有图像中该ROI均无效，请检查坐标。")
    combined = np.concatenate(all_pixels)
    raw_mean, robust_mean, valid_ratio = compute_iqr_robust_mean(combined)
    return RoiStats(
        x_min=x_min,
        y_min=y_min,
        x_max=x_max,
        y_max=y_max,
        raw_mean=raw_mean,
        robust_mean=robust_mean,
        valid_ratio=valid_ratio,
    )


def compute_multi_image_roi_stats_with_variance(images: list[np.ndarray], x_min: int, y_min: int, x_max: int, y_max: int) -> tuple[RoiStats, float]:
    """
    扩展版本：除了返回合并统计，还返回每张图像稳健均值之间的标准差（用于评估多图一致性）
    """
    per_image_robust = []
    for img in images:
        roi = img[y_min:y_max, x_min:x_max]
        if roi.size == 0:
            continue
        _, robust_mean, _ = compute_iqr_robust_mean(roi)
        per_image_robust.append(robust_mean)
    
    if not per_image_robust:
        raise ValueError("所有图像中该ROI均无效，请检查坐标。")
    
    # 计算多图合并后的总体统计
    combined_stats = compute_multi_image_roi_stats(images, x_min, y_min, x_max, y_max)
    # 计算各图像稳健均值的标准差（反映序列间波动）
    std_across_images = float(np.std(per_image_robust, ddof=1)) if len(per_image_robust) > 1 else 0.0
    return combined_stats, std_across_images


def exponential_model(thickness: np.ndarray, a: float, b: float, c: float) -> np.ndarray:
    return a * np.exp(-b * thickness) + c


def normalize_for_preview(image: np.ndarray) -> np.ndarray:
    low, high = np.percentile(image, [1, 99])
    if math.isclose(float(high), float(low)):
        return np.zeros_like(image, dtype=np.float32)
    normalized = (image.astype(np.float32) - float(low)) / float(high - low)
    return np.clip(normalized, 0.0, 1.0)


def select_dicom_folder() -> Path | None:
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        folder_path = filedialog.askdirectory(
            title="请选择包含 DICOM 文件的文件夹",
        )
    finally:
        root.destroy()

    if not folder_path:
        return None
    return Path(folder_path).expanduser().resolve()


def get_dicom_files_from_folder(folder_path: Path) -> list[Path]:
    if not folder_path.is_dir():
        raise NotADirectoryError(f"无效的文件夹路径: {folder_path}")
    files = sorted(folder_path.glob("*.dcm"))
    if not files:
        raise FileNotFoundError(f"在文件夹 {folder_path} 中未找到任何 .dcm 文件。")
    return files


def fit_calibration_curve(thicknesses: np.ndarray, gray_values: np.ndarray) -> tuple[np.ndarray, dict[str, float]]:
    amplitude_guess = max(float(gray_values.max() - gray_values.min()), 1.0)
    baseline_guess = float(gray_values.min())
    p0 = [amplitude_guess, 2.0, baseline_guess]
    bounds = ([0.0, 1e-6, -np.inf], [np.inf, np.inf, np.inf])

    params, _ = curve_fit(
        exponential_model,
        thicknesses,
        gray_values,
        p0=p0,
        bounds=bounds,
        maxfev=10000,
    )

    predicted = exponential_model(thicknesses, *params)
    ss_res = float(np.sum((gray_values - predicted) ** 2))
    ss_tot = float(np.sum((gray_values - np.mean(gray_values)) ** 2))
    # 计算RMSE
    rmse = float(np.sqrt(ss_res / len(thicknesses)))
    stats = {
        "r2": 1.0 if math.isclose(ss_tot, 0.0) else 1.0 - ss_res / ss_tot,
        "mae": float(np.mean(np.abs(gray_values - predicted))),
        "max_err": float(np.max(np.abs(gray_values - predicted))),
        "rmse": rmse,
    }
    return params.astype(np.float64), stats


def estimate_lead_equivalent(
    gray_value: float,
    fit_params: np.ndarray,
    calibration_range: tuple[float, float],
) -> tuple[float, list[str], bool]:
    a, b, c = [float(x) for x in fit_params]
    warnings: list[str] = []

    numerator = gray_value - c
    if numerator <= 0:
        raise ValueError(f"灰度值 {gray_value:.3f} 小于等于拟合基线 {c:.3f}，无法可靠反算。")

    ratio = numerator / a
    if ratio >= 1:
        raise ValueError(
            f"灰度值 {gray_value:.3f} 高于标定曲线的起始灰度，计算结果会得到负铅当量。"
        )

    thickness = -math.log(ratio) / b

    lower, upper = calibration_range
    is_extrapolated = thickness < lower or thickness > upper
    if is_extrapolated:
        warnings.append(
            "该结果超出当前标定块范围 0.15~0.75 mmPb，已按拟合曲线进行扩展计算。"
        )
        warnings.append(f"扩展计算结果 = {thickness:.4f} mmPb")

    return thickness, warnings, is_extrapolated


def build_result_text(
    roi_stats: RoiStats,
    stage: str,
    lead_equivalent: float | None = None,
    warnings: list[str] | None = None,
    thickness_label: float | None = None,
    preview_path: Path | None = None,
    is_extrapolated: bool = False,
) -> str:
    lines = [
        f"{stage} ROI: x=[{roi_stats.x_min}:{roi_stats.x_max}], y=[{roi_stats.y_min}:{roi_stats.y_max}]",
        f"原始均值={roi_stats.raw_mean:.3f} | 稳健均值={roi_stats.robust_mean:.3f} | 有效像素占比={roi_stats.valid_ratio:.2%}",
    ]
    if thickness_label is not None:
        lines.append(f"标定厚度={thickness_label:.3f} mmPb")
    if lead_equivalent is not None:
        prefix = "扩展计算铅当量" if is_extrapolated else "换算铅当量"
        lines.append(f"{prefix}={lead_equivalent:.4f} mmPb")
    if warnings:
        lines.append("提示: " + " | ".join(warnings))
    if preview_path is not None:
        lines.append(f"预览图={preview_path.name}")
    return "\n".join(lines)


class LeadEquivalentApp:
    def __init__(self, dicom_paths: list[Path]) -> None:
        self.dicom_paths = dicom_paths
        self.images = [load_dicom_image(p) for p in dicom_paths]
        shapes = [img.shape for img in self.images]
        if not all(s == shapes[0] for s in shapes):
            raise ValueError("所有 DICOM 图像尺寸不一致，无法使用相同坐标的 ROI。请检查文件。")
        self.height, self.width = self.images[0].shape

        self.display_min, self.display_max = np.percentile(self.images[0], [1, 99])
        self.output_dir = self.dicom_paths[0].resolve().parent
        self.preview_dir = self.output_dir / "roi_previews"
        self.preview_dir.mkdir(exist_ok=True)
        self.csv_path = self.output_dir / "roi_measurements.csv"

        self.mode = "calibration"
        self.calibration_rois: list[RoiStats] = []
        self.measurement_roi: RoiStats | None = None
        self.calibration_patches: list[Rectangle] = []
        self.calibration_labels = []
        self.measurement_patch: Rectangle | None = None
        self.fit_params: np.ndarray | None = None
        self.fit_stats: dict[str, float] | None = None
        # 存储最近一次测量的详细信息： (稳健均值, 铅当量, 多图标准差, 是否外推)
        self.last_measurement_detail: tuple[float, float, float, bool] | None = None

        self.figure = plt.figure(figsize=(14, 8))
        grid = self.figure.add_gridspec(
            nrows=1,
            ncols=2,
            width_ratios=[3, 2],
            left=0.04,
            right=0.98,
            top=0.90,
            bottom=0.15,
            wspace=0.18,
        )
        self.ax_image = self.figure.add_subplot(grid[0, 0])
        self.ax_curve = self.figure.add_subplot(grid[0, 1])

        self.status_text = self.figure.text(0.04, 0.95, "", fontsize=11)
        self.result_text = self.figure.text(0.52, 0.95, "", fontsize=10, va="top")

        self._draw_image_panel()
        self._draw_curve_panel()
        self._build_controls()
        self._update_status()

        self.selector = RectangleSelector(
            self.ax_image,
            self._on_select,
            useblit=True,
            button=[1],
            minspanx=5,
            minspany=5,
            spancoords="pixels",
            interactive=False,
        )
        self.figure.canvas.mpl_connect("key_press_event", self._on_key_press)

        print("使用说明：")
        print(f"已加载 {len(self.images)} 张 DICOM 图像。")
        print("1. 请按从左到右顺序手动框选上方 6 个标定块（0.15, 0.20, 0.40, 0.45, 0.50, 0.75 mmPb）。")
        print("2. 完成拟合后，在下部框选测量铅当量。")
        print("3. 按键 R 重置 | S 保存拟合图 | P 导出含结论的 PDF 报告")

    def _draw_image_panel(self) -> None:
        self.ax_image.clear()
        self.ax_image.imshow(
            self.images[0],
            cmap="gray",
            vmin=self.display_min,
            vmax=self.display_max,
        )
        self.ax_image.set_title(f"DICOM 图像 (共 {len(self.images)} 张): {self.dicom_paths[0].name}")
        self.ax_image.set_xlabel("X")
        self.ax_image.set_ylabel("Y")

    def _draw_curve_panel(self) -> None:
        self.ax_curve.clear()
        self.ax_curve.set_title("标定曲线")
        self.ax_curve.set_xlabel("铅当量 T (mmPb)")
        self.ax_curve.set_ylabel("灰度均值 G")
        self.ax_curve.grid(True, linestyle="--", alpha=0.4)

        if not self.calibration_rois:
            self.ax_curve.text(
                0.5,
                0.5,
                "等待标定 ROI",
                ha="center",
                va="center",
                transform=self.ax_curve.transAxes,
                fontsize=12,
            )
            self.figure.canvas.draw_idle()
            return

        gray_values = np.array([item.robust_mean for item in self.calibration_rois], dtype=np.float64)
        thicknesses = STANDARD_THICKNESSES[: len(self.calibration_rois)]
        self.ax_curve.scatter(thicknesses, gray_values, color="tab:red", s=65, label="标定点")

        if self.fit_params is not None and len(self.calibration_rois) == len(STANDARD_THICKNESSES):
            thickness_smooth = np.linspace(0.0, max(STANDARD_THICKNESSES) * 1.15, 200)
            gray_smooth = exponential_model(thickness_smooth, *self.fit_params)
            self.ax_curve.plot(thickness_smooth, gray_smooth, color="tab:blue", linewidth=2.0, label="拟合曲线")

            a, b, c = self.fit_params
            stats = self.fit_stats or {}
            info_lines = [
                f"G = {a:.2f} * exp(-{b:.4f} * T) + {c:.2f}",
                f"R^2 = {stats.get('r2', float('nan')):.5f}",
                f"RMSE = {stats.get('rmse', float('nan')):.3f}",
                f"MAE = {stats.get('mae', float('nan')):.3f}",
            ]
            self.ax_curve.text(
                0.03,
                0.97,
                "\n".join(info_lines),
                transform=self.ax_curve.transAxes,
                va="top",
                bbox={"facecolor": "white", "alpha": 0.85, "edgecolor": "gray"},
            )
            self.ax_curve.legend(loc="upper right")

        self.figure.canvas.draw_idle()

    def _build_controls(self) -> None:
        ax_reset = self.figure.add_axes([0.04, 0.04, 0.12, 0.06])
        ax_save = self.figure.add_axes([0.17, 0.04, 0.12, 0.06])
        ax_report = self.figure.add_axes([0.30, 0.04, 0.16, 0.06])

        self.btn_reset = Button(ax_reset, "重置标定")
        self.btn_save = Button(ax_save, "保存拟合图")
        self.btn_report = Button(ax_report, "导出PDF报告")

        self.btn_reset.on_clicked(lambda _event: self.reset_calibration())
        self.btn_save.on_clicked(lambda _event: self.save_curve_plot())
        self.btn_report.on_clicked(lambda _event: self.export_pdf_report())

    def _on_key_press(self, event) -> None:
        if event.key in {"r", "R"}:
            self.reset_calibration()
        elif event.key in {"s", "S"}:
            self.save_curve_plot()
        elif event.key in {"p", "P"}:
            self.export_pdf_report()

    def _on_select(self, eclick, erelease) -> None:
        if None in (eclick.xdata, eclick.ydata, erelease.xdata, erelease.ydata):
            return

        x_min = int(np.clip(min(eclick.xdata, erelease.xdata), 0, self.width - 1))
        x_max = int(np.clip(max(eclick.xdata, erelease.xdata), 0, self.width))
        y_min = int(np.clip(min(eclick.ydata, erelease.ydata), 0, self.height - 1))
        y_max = int(np.clip(max(eclick.ydata, erelease.ydata), 0, self.height))
        if x_max <= x_min or y_max <= y_min:
            return

        try:
            roi_stats = compute_multi_image_roi_stats(self.images, x_min, y_min, x_max, y_max)
        except Exception as e:
            print(f"ROI 统计出错: {e}")
            return

        if self.mode == "calibration":
            self._append_calibration_roi(roi_stats)
        else:
            self._measure_roi(roi_stats)

    def _append_calibration_roi(self, roi_stats: RoiStats, note: str | None = None) -> None:
        index = len(self.calibration_rois)
        if index >= len(STANDARD_THICKNESSES):
            print("已收集完所有标定块，请切换到测量模式。")
            return
        thickness = STANDARD_THICKNESSES[index]
        self.calibration_rois.append(roi_stats)
        preview_path = self._save_roi_preview(
            roi_stats=roi_stats,
            stage="calibration",
            file_stem=f"calibration_{index + 1:02d}_{thickness:.3f}mmPb",
        )
        self._append_result_record(
            stage="calibration",
            roi_stats=roi_stats,
            preview_path=preview_path,
            thickness_label=thickness,
            lead_equivalent=None,
            note=note or f"标定块 {index + 1} (多图平均)",
        )
        message = build_result_text(
            roi_stats=roi_stats,
            stage=f"标定 {index + 1}/{len(STANDARD_THICKNESSES)}",
            thickness_label=thickness,
            preview_path=preview_path,
        )
        print(message)

        rect = Rectangle(
            (roi_stats.x_min, roi_stats.y_min),
            roi_stats.x_max - roi_stats.x_min,
            roi_stats.y_max - roi_stats.y_min,
            fill=False,
            edgecolor="tab:orange",
            linewidth=2,
        )
        self.ax_image.add_patch(rect)
        self.calibration_patches.append(rect)

        label = self.ax_image.text(
            roi_stats.x_min,
            roi_stats.y_min - 10,
            f"{thickness:.3f} mmPb",
            color="tab:orange",
            fontsize=9,
            bbox={"facecolor": "white", "alpha": 0.7, "edgecolor": "none"},
        )
        self.calibration_labels.append(label)

        if len(self.calibration_rois) == len(STANDARD_THICKNESSES):
            self.fit_params, self.fit_stats = fit_calibration_curve(
                STANDARD_THICKNESSES,
                np.array([item.robust_mean for item in self.calibration_rois], dtype=np.float64),
            )
            self.mode = "measurement"
            a, b, c = self.fit_params
            print("标定完成: 当前测量计算使用统一指数拟合曲线")
            print(f"拟合曲线: G = {a:.3f} * exp(-{b:.5f} * T) + {c:.3f}")
            print(
                f"拟合指标: R^2={self.fit_stats['r2']:.5f}, "
                f"RMSE={self.fit_stats['rmse']:.3f}, MAE={self.fit_stats['mae']:.3f}"
            )

        self._draw_curve_panel()
        self.result_text.set_text(message)
        self._update_status()

    def _measure_roi(self, roi_stats: RoiStats) -> None:
        if self.fit_params is None:
            return

        if self.measurement_patch is not None:
            self.measurement_patch.remove()
            self.measurement_patch = None

        # 使用带方差计算的扩展函数
        try:
            combined_stats, std_across_images = compute_multi_image_roi_stats_with_variance(
                self.images, roi_stats.x_min, roi_stats.y_min, roi_stats.x_max, roi_stats.y_max
            )
        except Exception as e:
            print(f"ROI 统计出错: {e}")
            return

        # 保留传入的 roi_stats 用于显示（但用 combined_stats 的统计值覆盖）
        roi_stats.raw_mean = combined_stats.raw_mean
        roi_stats.robust_mean = combined_stats.robust_mean
        roi_stats.valid_ratio = combined_stats.valid_ratio

        rect = Rectangle(
            (roi_stats.x_min, roi_stats.y_min),
            roi_stats.x_max - roi_stats.x_min,
            roi_stats.y_max - roi_stats.y_min,
            fill=False,
            edgecolor="lime",
            linewidth=2,
        )
        self.ax_image.add_patch(rect)
        self.measurement_patch = rect
        self.measurement_roi = roi_stats
        preview_path = self._save_roi_preview(
            roi_stats=roi_stats,
            stage="measurement",
            file_stem=f"measurement_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        )

        try:
            thickness, warnings, is_extrapolated = estimate_lead_equivalent(
                gray_value=roi_stats.robust_mean,
                fit_params=self.fit_params,
                calibration_range=(float(STANDARD_THICKNESSES.min()), float(STANDARD_THICKNESSES.max())),
            )
            # 存储详细信息 (灰度, 铅当量, 多图标准差, 是否外推)
            self.last_measurement_detail = (roi_stats.robust_mean, thickness, std_across_images, is_extrapolated)

            message = build_result_text(
                roi_stats=roi_stats,
                stage="测量结果",
                lead_equivalent=thickness,
                warnings=warnings,
                preview_path=preview_path,
                is_extrapolated=is_extrapolated,
            )
            note = "扩展计算" if is_extrapolated else ""
            if warnings:
                note = " | ".join([text for text in [note, *warnings] if text])
            self._append_result_record(
                stage="measurement",
                roi_stats=roi_stats,
                preview_path=preview_path,
                thickness_label=None,
                lead_equivalent=thickness,
                note=note,
            )
        except ValueError as exc:
            self.last_measurement_detail = None
            message = build_result_text(
                roi_stats=roi_stats,
                stage="测量结果",
                warnings=[f"无法可靠换算: {exc}"],
                preview_path=preview_path,
            )
            self._append_result_record(
                stage="measurement",
                roi_stats=roi_stats,
                preview_path=preview_path,
                thickness_label=None,
                lead_equivalent=None,
                note=str(exc),
            )

        print(message)
        self.result_text.set_text(message)
        self.figure.canvas.draw_idle()

    def _update_status(self) -> None:
        if self.mode == "calibration":
            index = len(self.calibration_rois)
            next_thickness = STANDARD_THICKNESSES[index]
            status = (
                "步骤 1/2: 手动框选上方标定块 | "
                f"当前需要框选第 {index + 1} 个，对应 {next_thickness:.3f} mmPb"
            )
            if not self.calibration_rois:
                self.result_text.set_text(
                    "结果区: 请按从左到右顺序手动框选 6 个标定块（0.15, 0.20, 0.40, 0.45, 0.50, 0.75 mmPb）。\n"
                    "导出文件: roi_measurements.csv\n"
                    "预览目录: roi_previews"
                )
        else:
            status = "步骤 2/2: 标定完成，请在图像下部任意区域框选，程序将计算铅当量。"
        self.status_text.set_text(status)
        self.figure.canvas.draw_idle()

    def reset_calibration(self) -> None:
        self.mode = "calibration"
        self.calibration_rois.clear()
        self.measurement_roi = None
        self.fit_params = None
        self.fit_stats = None
        self.last_measurement_detail = None
        self.result_text.set_text("结果区: 已重置，请重新标定。\n导出文件保留在 roi_measurements.csv\n预览目录保留在 roi_previews")

        for patch in self.calibration_patches:
            patch.remove()
        self.calibration_patches.clear()

        for label in self.calibration_labels:
            label.remove()
        self.calibration_labels.clear()

        if self.measurement_patch is not None:
            self.measurement_patch.remove()
            self.measurement_patch = None

        self._draw_curve_panel()
        self._update_status()
        print("已重置标定。请按顺序手动框选 6 个标定块。")

    def save_curve_plot(self) -> None:
        output_path = self.dicom_paths[0].with_name("calibration_plot.png")
        self.figure.savefig(output_path, dpi=160)
        print(f"已保存拟合图: {output_path}")
        self.result_text.set_text(f"拟合图已保存到: {output_path}")
        self.figure.canvas.draw_idle()

    # ================= 新增核心功能：含扩展结论的PDF报告 =================
    def export_pdf_report(self) -> None:
        """生成一份包含诊断结论的PDF报告"""
        if self.fit_params is None:
            print("警告: 标定尚未完成，请先完成6个标定块的框选再进行导出。")
            self.result_text.set_text("错误: 标定未完成，无法导出报告。")
            self.figure.canvas.draw_idle()
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = self.output_dir / f"report_{timestamp}.pdf"

        with PdfPages(report_path) as pdf:
            fig_report = plt.figure(figsize=(11.69, 8.27), dpi=150)

            # 1. 左侧：DICOM图像
            ax_img = fig_report.add_axes([0.05, 0.15, 0.40, 0.70])
            ax_img.imshow(self.images[0], cmap="gray", vmin=self.display_min, vmax=self.display_max)
            for i, roi in enumerate(self.calibration_rois):
                rect = Rectangle(
                    (roi.x_min, roi.y_min),
                    roi.x_max - roi.x_min,
                    roi.y_max - roi.y_min,
                    fill=False,
                    edgecolor="tab:orange",
                    linewidth=1.5,
                )
                ax_img.add_patch(rect)
                thickness = STANDARD_THICKNESSES[i]
                ax_img.text(
                    roi.x_min,
                    roi.y_min - 8,
                    f"{thickness:.3f}mm",
                    color="tab:orange",
                    fontsize=8,
                    bbox={"facecolor": "white", "alpha": 0.6, "edgecolor": "none"},
                )
            if self.measurement_patch is not None and self.measurement_roi is not None:
                rect_meas = Rectangle(
                    (self.measurement_roi.x_min, self.measurement_roi.y_min),
                    self.measurement_roi.x_max - self.measurement_roi.x_min,
                    self.measurement_roi.y_max - self.measurement_roi.y_min,
                    fill=False,
                    edgecolor="lime",
                    linewidth=2,
                )
                ax_img.add_patch(rect_meas)
            ax_img.set_title(f"原始图像 (共 {len(self.images)} 张平均)")
            ax_img.axis("off")

            # 2. 右侧：拟合曲线
            ax_curve = fig_report.add_axes([0.55, 0.50, 0.40, 0.35])
            gray_values = np.array([item.robust_mean for item in self.calibration_rois], dtype=np.float64)
            thicknesses = STANDARD_THICKNESSES[: len(self.calibration_rois)]
            ax_curve.scatter(thicknesses, gray_values, color="tab:red", s=80, label="标定点")
            if self.fit_params is not None:
                t_smooth = np.linspace(0.0, max(STANDARD_THICKNESSES) * 1.15, 200)
                g_smooth = exponential_model(t_smooth, *self.fit_params)
                ax_curve.plot(t_smooth, g_smooth, color="tab:blue", linewidth=2.5, label="拟合曲线")
            ax_curve.set_xlabel("铅当量 T (mmPb)", fontsize=10)
            ax_curve.set_ylabel("灰度均值 G", fontsize=10)
            ax_curve.grid(True, linestyle="--", alpha=0.4)
            ax_curve.legend(loc="best")

            # 3. 右侧下方：扩展结论区
            info_text_x = 0.55
            info_text_y = 0.08

            a, b, c = self.fit_params
            stats = self.fit_stats or {}
            rmse = stats.get('rmse', 0.0)
            r2 = stats.get('r2', 0.0)

            # 构造结论文本
            conclusion_lines = [
                "【标定参数】",
                f"拟合公式: G = {a:.3f}·exp(-{b:.5f}·T) + {c:.3f}",
                f"决定系数 R² = {r2:.6f}  (拟合优度{'极佳' if r2 > 0.995 else '良好' if r2 > 0.98 else '一般'})",
                f"残差 RMSE = {rmse:.4f}  (模型预测精度)",
                "",
            ]

            if self.last_measurement_detail is not None:
                gray_val, lead_eq, std_img, is_extrapolated = self.last_measurement_detail
                
                # 计算扩展不确定度 (k=2)
                # 随机不确定度来源于多图间灰度标准差，系统不确定度来源于RMSE
                # 将灰度误差转换为铅当量误差：近似 dT = dG / (a * b * exp(-b*T))
                if lead_eq > 0:
                    # 导数 dG/dT = -a * b * exp(-b*T) = -(a - c) * b? 为了简化，直接用数值差分近似
                    # 使用当前点斜率：slope = -a * b * exp(-b * lead_eq)
                    slope_abs = a * b * math.exp(-b * lead_eq)
                    if slope_abs > 1e-9:
                        # 灰度误差 = 多图标准差 + 拟合RMSE (综合)
                        gray_uncertainty = std_img + rmse
                        lead_uncertainty = gray_uncertainty / slope_abs
                        expanded_uncertainty = 2 * lead_uncertainty  # k=2
                    else:
                        expanded_uncertainty = 0.0
                else:
                    expanded_uncertainty = 0.0

                conclusion_lines.append("【测量结论】")
                conclusion_lines.append(f"被测区域等效铅当量: {lead_eq:.5f} ± {expanded_uncertainty:.5f} mmPb (扩展不确定度, k=2)")
                if is_extrapolated:
                    conclusion_lines.append("⚠️ 警告: 该结果位于标定范围之外，属于外推计算，精度可能下降。")
                else:
                    conclusion_lines.append("✓ 结果在有效标定范围内，数据可靠。")

                # 多图稳定性评价
                if std_img < 1.0:
                    stability = "优秀 (序列间灰度波动极小)"
                elif std_img < 3.0:
                    stability = "良好"
                else:
                    stability = "一般 (建议检查图像配准或噪声)"
                conclusion_lines.append(f"多图数据稳定性: {stability} (灰度标准差 {std_img:.3f})")

                # 屏蔽性能自动评级 (基于常用标准)
                conclusion_lines.append("")
                conclusion_lines.append("【屏蔽性能评级】")
                if lead_eq >= 0.75:
                    grade = "★★★★★ (极佳，可满足高能X射线防护要求)"
                elif lead_eq >= 0.50:
                    grade = "★★★★☆ (优秀，适用于常规诊断X射线防护)"
                elif lead_eq >= 0.25:
                    grade = "★★★☆☆ (良好，满足一般防护需求)"
                else:
                    grade = "★★☆☆☆ (较低，建议增加屏蔽层厚度)"
                conclusion_lines.append(f"评级: {grade}")
            else:
                conclusion_lines.append("【测量结论】")
                conclusion_lines.append("尚未进行测量框选，请框选待测区域以获取铅当量结论。")

            # 绘制结论文本框
            fig_report.text(
                info_text_x,
                info_text_y,
                "\n".join(conclusion_lines),
                fontsize=9.5,
                va="bottom",
                bbox={"facecolor": "#eef6ff", "alpha": 0.95, "edgecolor": "#2c3e50", 
                      "boxstyle": "round,pad=0.6", "linewidth": 1.5},
            )

            # 报告标题
            fig_report.text(
                0.5,
                0.96,
                "铅当量测量分析报告 (含诊断结论)",
                ha="center",
                fontsize=18,
                fontweight="bold",
            )
            fig_report.text(
                0.5,
                0.92,
                f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  文件: {self.dicom_paths[0].name}",
                ha="center",
                fontsize=10,
                color="gray",
            )

            pdf.savefig(fig_report)
            plt.close(fig_report)

        print(f"PDF 报告已成功导出: {report_path}")
        self.result_text.set_text(f"PDF 报告已保存到: {report_path.name}")
        self.figure.canvas.draw_idle()

    # ================= 辅助函数 =================
    def _save_roi_preview(self, roi_stats: RoiStats, stage: str, file_stem: str) -> Path:
        first_img = self.images[0]
        roi = first_img[roi_stats.y_min:roi_stats.y_max, roi_stats.x_min:roi_stats.x_max]
        normalized_full = normalize_for_preview(first_img)
        normalized_roi = normalize_for_preview(roi)
        output_path = self.preview_dir / f"{file_stem}.png"

        preview_figure, (ax_full, ax_roi) = plt.subplots(1, 2, figsize=(10, 4.5))
        ax_full.imshow(normalized_full, cmap="gray", vmin=0.0, vmax=1.0)
        ax_full.add_patch(
            Rectangle(
                (roi_stats.x_min, roi_stats.y_min),
                roi_stats.x_max - roi_stats.x_min,
                roi_stats.y_max - roi_stats.y_min,
                fill=False,
                edgecolor="red" if stage == "calibration" else "lime",
                linewidth=2,
            )
        )
        ax_full.set_title(f"{stage} 全图定位")
        ax_full.axis("off")

        ax_roi.imshow(normalized_roi, cmap="gray", vmin=0.0, vmax=1.0)
        ax_roi.set_title(
            "ROI 预览 (多图平均)\n"
            f"Raw={roi_stats.raw_mean:.1f}  Robust={roi_stats.robust_mean:.1f}\n"
            f"Valid={roi_stats.valid_ratio:.2%}"
        )
        ax_roi.axis("off")

        preview_figure.tight_layout()
        preview_figure.savefig(output_path, dpi=160)
        plt.close(preview_figure)
        return output_path

    def _append_result_record(
        self,
        stage: str,
        roi_stats: RoiStats,
        preview_path: Path,
        thickness_label: float | None,
        lead_equivalent: float | None,
        note: str,
    ) -> None:
        file_exists = self.csv_path.exists()
        with self.csv_path.open("a", newline="", encoding="utf-8-sig") as csv_file:
            writer = csv.writer(csv_file)
            if not file_exists:
                writer.writerow(
                    [
                        "timestamp",
                        "stage",
                        "roi_x_min",
                        "roi_y_min",
                        "roi_x_max",
                        "roi_y_max",
                        "raw_mean",
                        "robust_mean",
                        "valid_ratio",
                        "calibration_thickness_mmPb",
                        "lead_equivalent_mmPb",
                        "preview_file",
                        "note",
                    ]
                )
            full_note = f"images={len(self.images)}; " + note if note else f"images={len(self.images)}"
            writer.writerow(
                [
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    stage,
                    roi_stats.x_min,
                    roi_stats.y_min,
                    roi_stats.x_max,
                    roi_stats.y_max,
                    f"{roi_stats.raw_mean:.6f}",
                    f"{roi_stats.robust_mean:.6f}",
                    f"{roi_stats.valid_ratio:.6f}",
                    "" if thickness_label is None else f"{thickness_label:.6f}",
                    "" if lead_equivalent is None else f"{lead_equivalent:.6f}",
                    preview_path.name,
                    full_note,
                ]
            )

    def run(self) -> None:
        plt.show()


def resolve_dicom_folder(input_path: str | None) -> Path:
    if input_path:
        folder_path = Path(input_path).expanduser().resolve()
        if not folder_path.is_dir():
            raise NotADirectoryError(f"找不到文件夹: {folder_path}")
        return folder_path

    selected_path = select_dicom_folder()
    if selected_path is not None:
        return selected_path

    raise FileNotFoundError("未选择文件夹，程序已取消启动。")


def main() -> None:
    parser = argparse.ArgumentParser(description="DICOM 灰度标定与铅当量计算工具（多图、文件夹输入）")
    parser.add_argument(
        "folder_path",
        nargs="?",
        help="包含 DICOM 文件的文件夹路径；不传时弹出文件夹选择对话框",
    )
    args = parser.parse_args()

    folder_path = resolve_dicom_folder(args.folder_path)
    dicom_files = get_dicom_files_from_folder(folder_path)

    print(f"在文件夹 {folder_path} 中找到 {len(dicom_files)} 个 DCM 文件。")
    app = LeadEquivalentApp(dicom_files)
    app.run()


if __name__ == "__main__":
    main()
