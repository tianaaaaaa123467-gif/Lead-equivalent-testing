// 全局状态
let state = {
    imageData: null,        // base64 图像
    images: [],             // 所有图像base64（暂存，用于切换）
    currentImageIndex: 0,
    width: 0,
    height: 0,
    thicknesses: [],
    nextThickness: 0,
    mode: 'calibration',    // 'calibration' | 'measurement'
    calibrationPoints: [],  // {x, y, w, h, thickness}
    fitParams: null,
    fitStats: null,
    curveChart: null,
    measurementResult: null,
};

// DOM 元素
const canvas = document.getElementById('imageCanvas');
const ctx = canvas.getContext('2d');
const fileInput = document.getElementById('fileInput');
const uploadForm = document.getElementById('uploadForm');
const uploadBtn = document.getElementById('uploadBtn');
const fileCount = document.getElementById('fileCount');
const canvasTip = document.getElementById('canvasTip');
const statusBar = document.getElementById('statusBar');
const resultContent = document.getElementById('resultContent');
const curveStats = document.getElementById('curveStats');
const prevBtn = document.getElementById('prevImageBtn');
const nextBtn = document.getElementById('nextImageBtn');
const imageIndexDisplay = document.getElementById('imageIndexDisplay');
const resetBtn = document.getElementById('resetBtn');
const exportPdfBtn = document.getElementById('exportPdfBtn');

// 框选相关
let isDrawing = false;
let startX = 0, startY = 0, endX = 0, endY = 0;
let drawnRectangles = []; // 存储已绘制的标定框 {x, y, w, h, label}

// ---------- 工具函数 ----------
function setStatus(msg, isError = false) {
    statusBar.textContent = msg;
    statusBar.style.color = isError ? '#c0392b' : '#1a3a6b';
}

function updateNavStatus(text) {
    document.getElementById('navStatus').textContent = text;
}

// 显示提示信息
function showResult(html) {
    resultContent.innerHTML = html;
}

// ---------- Canvas 绘制 ----------
function drawCanvas(imageBase64, rects = []) {
    if (!imageBase64) return;
    const img = new Image();
    img.onload = function() {
        // 保持 canvas 尺寸与显示一致（CSS 缩放）
        const canvasWidth = canvas.clientWidth;
        const scale = canvasWidth / img.width;
        canvas.width = img.width;
        canvas.height = img.height;
        // 绘制图像
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);

        // 绘制已有矩形
        rects.forEach(r => {
            ctx.strokeStyle = r.color || '#ff8c00';
            ctx.lineWidth = 2;
            ctx.strokeRect(r.x, r.y, r.w, r.h);
            if (r.label) {
                ctx.fillStyle = 'rgba(255,255,255,0.7)';
                ctx.fillRect(r.x, r.y - 20, 80, 18);
                ctx.fillStyle = '#1a3a6b';
                ctx.font = '12px sans-serif';
                ctx.fillText(r.label, r.x + 4, r.y - 6);
            }
        });

        // 如果当前正在框选，画临时矩形
        if (isDrawing && endX && endY) {
            ctx.strokeStyle = '#00ccff';
            ctx.lineWidth = 1.5;
            ctx.setLineDash([5, 5]);
            ctx.strokeRect(
                Math.min(startX, endX),
                Math.min(startY, endY),
                Math.abs(endX - startX),
                Math.abs(endY - startY)
            );
            ctx.setLineDash([]);
        }
    };
    img.src = 'data:image/png;base64,' + imageBase64;
}

// 更新图像显示（切换图像时）
function updateImageDisplay() {
    if (state.images.length === 0) return;
    const idx = state.currentImageIndex;
    const imgData = state.images[idx];
    imageIndexDisplay.textContent = `${idx+1} / ${state.images.length}`;
    // 重新绘制所有已保存的矩形
    const rects = drawnRectangles.map(r => ({
        x: r.x, y: r.y, w: r.w, h: r.h,
        label: r.label,
        color: r.color || '#ff8c00'
    }));
    // 如果是测量模式且有测量框，也画上（存储在 state.measurementRect）
    if (state.measurementRect) {
        rects.push({
            x: state.measurementRect.x,
            y: state.measurementRect.y,
            w: state.measurementRect.w,
            h: state.measurementRect.h,
            color: '#00ff00',
            label: '测量'
        });
    }
    drawCanvas(imgData, rects);
}

// ---------- 与后端通信 ----------
async function fetchAPI(url, method = 'POST', body = null) {
    const options = { method, headers: { 'Content-Type': 'application/json' } };
    if (body) options.body = JSON.stringify(body);
    const res = await fetch(url, options);
    if (!res.ok) {
        const err = await res.json();
        throw new Error(err.error || '请求失败');
    }
    return await res.json();
}

// 上传文件
uploadForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const files = fileInput.files;
    if (files.length === 0) {
        setStatus('请至少选择一个文件', true);
        return;
    }
    const formData = new FormData();
    for (let f of files) formData.append('dicom_files', f);

    setStatus('正在上传并处理...');
    uploadBtn.disabled = true;
    try {
        const res = await fetch('/upload', { method: 'POST', body: formData });
        const data = await res.json();
        if (data.error) throw new Error(data.error);

        // 更新状态
        state.imageData = data.image_data;
        state.images = [data.image_data]; // 目前后端只返回第一张，可扩展多张
        state.width = data.width;
        state.height = data.height;
        state.thicknesses = data.thicknesses;
        state.nextThickness = data.next_thickness;
        state.currentImageIndex = 0;
        state.calibrationPoints = [];
        state.mode = 'calibration';
        drawnRectangles = [];
        state.measurementRect = null;
        state.fitParams = null;
        state.fitStats = null;
        state.curveChart = null;

        // 更新界面
        updateNavStatus(`已加载 ${data.num_images} 张图像`);
        fileCount.textContent = `${files.length} 个文件`;
        canvasTip.textContent = `请按顺序框选标定块 (${data.next_thickness} mmPb)`;
        setStatus(`加载成功，请开始标定 (第1个标定块 ${data.next_thickness} mmPb)`);
        showResult(`<p class="hint">标定阶段：请框选第1个标定块 (${data.next_thickness} mmPb)</p>`);
        // 重置曲线图
        if (state.curveChart) {
            state.curveChart.destroy();
            state.curveChart = null;
        }
        curveStats.textContent = '等待标定数据...';
        // 绘制图像
        drawCanvas(data.image_data, []);
        // 启用交互
        canvas.style.cursor = 'crosshair';
    } catch (err) {
        setStatus('上传失败: ' + err.message, true);
    } finally {
        uploadBtn.disabled = false;
    }
});

// 发送框选的ROI
async function sendROI(xMin, yMin, xMax, yMax) {
    const endpoint = state.mode === 'calibration' ? '/add_calibration_roi' : '/measure';
    const payload = { x_min: Math.round(xMin), y_min: Math.round(yMin), x_max: Math.round(xMax), y_max: Math.round(yMax) };
    try {
        const data = await fetchAPI(endpoint, 'POST', payload);
        if (data.error) throw new Error(data.error);

        if (state.mode === 'calibration') {
            // 处理标定响应
            const idx = state.calibrationPoints.length + 1;
            const thickness = state.thicknesses[idx-1];
            // 记录框选矩形
            const rect = {
                x: Math.round(xMin),
                y: Math.round(yMin),
                w: Math.round(xMax - xMin),
                h: Math.round(yMax - yMin),
                label: `${thickness.toFixed(3)} mm`,
                color: '#ff8c00'
            };
            drawnRectangles.push(rect);
            state.calibrationPoints.push(rect);

            if (data.status === 'calibration_done') {
                // 标定完成
                state.mode = 'measurement';
                state.fitParams = data.curve_data.params;
                state.fitStats = data.curve_data.stats;
                // 绘制曲线
                drawCurve(data.curve_data);
                canvasTip.textContent = '标定完成！请框选测量区域';
                setStatus('标定完成，进入测量模式');
                showResult(`<p>✅ 标定完成！</p><p>拟合公式: G = ${data.curve_data.params[0].toFixed(3)}·exp(-${data.curve_data.params[1].toFixed(5)}·T) + ${data.curve_data.params[2].toFixed(3)}</p><p>R² = ${data.curve_data.stats.r2.toFixed(6)}</p><p>请框选测量区域。`);
            } else {
                // 继续下一个标定
                const nextThick = data.next_thickness;
                canvasTip.textContent = `请框选第 ${idx+1} 个标定块 (${nextThick} mmPb)`;
                setStatus(`标定块 ${idx} 已记录，请框选下一个 (${nextThick} mmPb)`);
                showResult(`<p>✅ 标定块 ${idx} (${thickness} mmPb) 已记录</p><p>请框选第 ${idx+1} 个标定块 (${nextThick} mmPb)</p>`);
                // 如果有曲线数据，更新
                if (data.curve_data) {
                    drawCurve(data.curve_data);
                }
            }
        } else {
            // 测量模式
            state.measurementRect = {
                x: Math.round(xMin),
                y: Math.round(yMin),
                w: Math.round(xMax - xMin),
                h: Math.round(yMax - yMin)
            };
            // 重新绘制图像
            updateImageDisplay();

            const result = data;
            state.measurementResult = result;
            let html = `<p><span class="lead-value">${result.lead_equivalent.toFixed(5)}</span> mmPb</p>`;
            if (result.uncertainty) {
                html += `<p>扩展不确定度 (k=2): ± ${result.uncertainty.toFixed(5)} mmPb</p>`;
            }
            html += `<p>稳健均值: ${result.robust_mean.toFixed(3)}</p>`;
            html += `<p>多图灰度标准差: ${result.std_across_images.toFixed(3)}</p>`;
            html += `<p>有效像素: ${(result.valid_ratio * 100).toFixed(1)}%</p>`;
            html += `<p>评级: <span class="grade">${result.grade}</span></p>`;
            if (result.is_extrapolated) {
                html += `<p style="color:#e67e22;">⚠️ 结果超出标定范围，属于外推计算</p>`;
            }
            if (result.warnings && result.warnings.length) {
                html += `<p style="color:#c0392b;">⚠️ ${result.warnings.join('; ')}</p>`;
            }
            showResult(html);
            setStatus('测量完成，可导出PDF报告');
        }
    } catch (err) {
        setStatus('操作失败: ' + err.message, true);
    }
}

// ---------- Canvas 鼠标事件 ----------
canvas.addEventListener('mousedown', (e) => {
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    startX = (e.clientX - rect.left) * scaleX;
    startY = (e.clientY - rect.top) * scaleY;
    isDrawing = true;
    endX = startX;
    endY = startY;
});

canvas.addEventListener('mousemove', (e) => {
    if (!isDrawing) return;
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    endX = (e.clientX - rect.left) * scaleX;
    endY = (e.clientY - rect.top) * scaleY;
    // 重绘图像和已有矩形，再画临时矩形
    updateImageDisplay();
    // 临时矩形由 drawCanvas 内部处理，需要传递 isDrawing 状态
    // 但我们直接用 drawCanvas 并传递当前框选坐标作为额外参数
    // 由于 drawCanvas 不支持动态临时，我们重新绘制全部
    // 为了方便，我们使用一个独立的绘制函数
    redrawWithTemp();
});

canvas.addEventListener('mouseup', () => {
    if (!isDrawing) return;
    isDrawing = false;

    // 规范化坐标（防止反向拖动：起点 > 终点）
    const xMin = Math.min(startX, endX);
    const yMin = Math.min(startY, endY);
    const xMax = Math.max(startX, endX);
    const yMax = Math.max(startY, endY);

    // 过滤极小框（避免单击误触发）
    const minSize = 15;
    if ((xMax - xMin) < minSize || (yMax - yMin) < minSize) {
        redrawWithTemp();
        return;
    }

    // 发送ROI数据（标定/测量自动区分）
    sendROI(xMin, yMin, xMax, yMax);
});


function redrawWithTemp() {
    // 重新绘制图像和所有矩形，再加上临时框
    if (!state.imageData) return;
    const img = new Image();
    img.onload = function() {
        const canvasWidth = canvas.clientWidth;
        const scale = canvasWidth / img.width;
        canvas.width = img.width;
        canvas.height = img.height;
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);

        // 画所有已保存矩形
        drawnRectangles.forEach(r => {
            ctx.strokeStyle = r.color || '#ff8c00';
            ctx.lineWidth = 2;
            ctx.strokeRect(r.x, r.y, r.w, r.h);
            if (r.label) {
                ctx.fillStyle = 'rgba(255,255,255,0.7)';
                ctx.fillRect(r.x, r.y - 20, 80, 18);
                ctx.fillStyle = '#1a3a6b';
                ctx.font = '12px sans-serif';
                ctx.fillText(r.label, r.x + 4, r.y - 6);
            }
        });
        if (state.measurementRect) {
            const mr = state.measurementRect;
            ctx.strokeStyle = '#00ff00';
            ctx.lineWidth = 2;
            ctx.strokeRect(mr.x, mr.y, mr.w, mr.h);
            ctx.fillStyle = 'rgba(0,255,0,0.1)';
            ctx.fillRect(mr.x, mr.y, mr.w, mr.h);
        }
        // 临时框
        if (isDrawing && endX && endY) {
            ctx.strokeStyle = '#00ccff';
            ctx.lineWidth = 1.5;
            ctx.setLineDash([5, 5]);
            ctx.strokeRect(
                Math.min(startX, endX),
                Math.min(startY, endY),
                Math.abs(endX - startX),
                Math.abs(endY - startY)
            );
            ctx.setLineDash([]);
        }
    };
    img.src = 'data:image/png;base64,' + state.imageData;
}

canvas.addEventListener('mouseup', () => {
    if (!isDrawing) return;
    isDrawing = false;
    // 计算有效矩形（至少10x10）
    const xMin = Math.min(startX, endX);
    const xMax = Math.max(startX, endX);
    const yMin = Math.min(startY, endY);
    const yMax = Math.max(startY, endY);
    if (xMax - xMin < 10 || yMax - yMin < 10) {
        setStatus('框选区域太小，请重新框选', true);
        return;
    }
    // 发送给后端
    sendROI(xMin, yMin, xMax, yMax);
    // 清除临时框并重绘
    redrawWithTemp();
});

// ---------- 曲线图绘制 (Chart.js) ----------
function drawCurve(curveData) {
    const ctxChart = document.getElementById('curveChart').getContext('2d');
    if (state.curveChart) {
        state.curveChart.destroy();
    }
    const t = curveData.t;
    const g = curveData.g;
    const points_t = curveData.points_t;
    const points_g = curveData.points_g;

    state.curveChart = new Chart(ctxChart, {
        type: 'scatter',
        data: {
            datasets: [
                {
                    label: '拟合曲线',
                    data: t.map((x, i) => ({ x: x, y: g[i] })),
                    type: 'line',
                    borderColor: '#1a3a6b',
                    borderWidth: 2,
                    fill: false,
                    pointRadius: 0,
                    tension: 0.1,
                },
                {
                    label: '标定点',
                    data: points_t.map((x, i) => ({ x: x, y: points_g[i] })),
                    backgroundColor: '#e74c3c',
                    pointRadius: 6,
                    pointHoverRadius: 8,
                    type: 'scatter',
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { position: 'top' },
                tooltip: { callbacks: { label: (ctx) => `(${ctx.parsed.x.toFixed(3)}, ${ctx.parsed.y.toFixed(2)})` } }
            },
            scales: {
                x: { title: { display: true, text: '铅当量 T (mmPb)' } },
                y: { title: { display: true, text: '灰度均值 G' } }
            }
        }
    });
    // 更新统计信息
    const stats = curveData.stats;
    curveStats.textContent = `R² = ${stats.r2.toFixed(6)}  |  RMSE = ${stats.rmse.toFixed(4)}  |  MAE = ${stats.mae.toFixed(4)}`;
}

// ---------- 切换图像 ----------
prevBtn.addEventListener('click', () => {
    if (state.images.length === 0) return;
    state.currentImageIndex = (state.currentImageIndex - 1 + state.images.length) % state.images.length;
    updateImageDisplay();
});

nextBtn.addEventListener('click', () => {
    if (state.images.length === 0) return;
    state.currentImageIndex = (state.currentImageIndex + 1) % state.images.length;
    updateImageDisplay();
});

// 重置标定
resetBtn.addEventListener('click', async () => {
    if (state.images.length === 0) {
        setStatus('没有加载图像，请先上传', true);
        return;
    }
    // 重置状态
    state.mode = 'calibration';
    state.calibrationPoints = [];
    drawnRectangles = [];
    state.measurementRect = null;
    state.fitParams = null;
    state.fitStats = null;
    state.measurementResult = null;
    if (state.curveChart) {
        state.curveChart.destroy();
        state.curveChart = null;
    }
    curveStats.textContent = '等待标定数据...';
    canvasTip.textContent = `请按顺序框选标定块 (${state.thicknesses[0]} mmPb)`;
    setStatus(`已重置，请框选第1个标定块 (${state.thicknesses[0]} mmPb)`);
    showResult(`<p class="hint">标定已重置，请框选第1个标定块 (${state.thicknesses[0]} mmPb)</p>`);
    // 重绘图像（只显示图像，无框）
    drawCanvas(state.imageData, []);
    // 同时重置曲线图
});

// 导出PDF
exportPdfBtn.addEventListener('click', async () => {
    if (!state.fitParams) {
        setStatus('请先完成标定', true);
        return;
    }
    try {
        setStatus('正在生成PDF...');
        const response = await fetch('/export_pdf');
        if (!response.ok) throw new Error('PDF生成失败');
        const blob = await response.blob();
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'report.pdf';
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.URL.revokeObjectURL(url);
        setStatus('PDF已导出');
    } catch (err) {
        setStatus('PDF导出失败: ' + err.message, true);
    }
});

// ---------- 初始化 ----------
setStatus('请上传DICOM文件开始');
canvasTip.textContent = '请上传图像';