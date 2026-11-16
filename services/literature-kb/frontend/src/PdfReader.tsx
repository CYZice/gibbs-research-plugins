import { useEffect, useRef, useState } from "react";
import type { PDFDocumentProxy } from "pdfjs-dist";
import { Icon } from "./icons";

let pdfjsPromise: Promise<typeof import("pdfjs-dist")> | undefined;
function loadPdfjs() {
  pdfjsPromise ??= import("pdfjs-dist").then((pdfjs) => {
    pdfjs.GlobalWorkerOptions.workerSrc = new URL("pdfjs-dist/build/pdf.worker.min.mjs", import.meta.url).toString();
    return pdfjs;
  });
  return pdfjsPromise;
}

export function PdfReader({ base, token, source }: { base: string; token: string; source: any }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const scroll = useRef<HTMLDivElement>(null);
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [pageNumber, setPage] = useState(1);
  const [zoom, setZoom] = useState(1);
  const [width, setWidth] = useState(600);
  const [loadError, setLoadError] = useState("");
  const [renderError, setRenderError] = useState("");
  const [rendered, setRendered] = useState(false);
  const location = source.citation?.location || source.location || {};
  const targetPage = location.page || source.page || 1;
  const assetId = source.pdf_asset_id || source.pdf?.asset_id;
  const url = source.pdf_url || (assetId ? `${base.replace(/\/$/, "")}/api/v1/assets/${assetId}/download` : "");
  const bbox = location.bbox || source.bbox;
  const validBbox = Array.isArray(bbox) && location.coordinate_space === "normalized" && bbox.length === 4
    && bbox.every((value: unknown) => typeof value === "number" && value >= 0 && value <= 1)
    && bbox[2] > bbox[0] && bbox[3] > bbox[1];
  const bboxKey = JSON.stringify(validBbox ? bbox : null);

  useEffect(() => {
    const node = scroll.current;
    if (!node) return;
    const observer = new ResizeObserver(() => setWidth(Math.max(200, node.clientWidth - 40)));
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    let alive = true;
    let loadingTask: ReturnType<typeof import("pdfjs-dist")["getDocument"]> | undefined;
    setPdf(null); setLoadError(""); setRendered(false);
    void (async () => {
      try {
        if (!url) throw new Error("原文未提供 PDF 文件");
        const pdfjs = await loadPdfjs();
        if (!alive) return;
        loadingTask = pdfjs.getDocument({ url, withCredentials: true, httpHeaders: token ? { Authorization: `Bearer ${token}` } : undefined });
        const document = await loadingTask.promise;
        if (alive) setPdf(document);
      } catch (error) {
        if (alive) setLoadError(error instanceof Error ? error.message : String(error));
      }
    })();
    return () => { alive = false; void loadingTask?.destroy(); };
  }, [url, token]);

  useEffect(() => {
    setPage(Math.min(pdf?.numPages || targetPage, Math.max(1, targetPage)));
    setZoom(1);
  }, [url, targetPage, pdf]);

  useEffect(() => {
    let alive = true;
    let task: { cancel: () => void; promise: Promise<void> } | undefined;
    setRenderError(""); setRendered(false);
    if (!pdf) return;
    void (async () => {
      try {
        const page = await pdf.getPage(pageNumber);
        if (!alive || !canvas.current) return;
        const natural = page.getViewport({ scale: 1 });
        const scale = width / natural.width * zoom;
        const viewport = page.getViewport({ scale });
        const density = Math.min(window.devicePixelRatio || 1, 2);
        const element = canvas.current;
        const context = element.getContext("2d");
        if (!context) throw new Error("浏览器不支持画布");
        element.width = Math.floor(viewport.width * density);
        element.height = Math.floor(viewport.height * density);
        element.style.width = `${viewport.width}px`;
        element.style.height = `${viewport.height}px`;
        task = page.render({ canvas: element, canvasContext: context, viewport, transform: [density, 0, 0, density, 0, 0] } as any);
        await task.promise;
        if (!alive) return;
        if (validBbox && pageNumber === targetPage) {
          const [x0, y0, x1, y1] = bbox;
          context.save();
          context.fillStyle = "rgba(65,118,230,.18)";
          context.strokeStyle = "#4176e6";
          context.lineWidth = 2 * density;
          context.fillRect(x0 * element.width, y0 * element.height, (x1 - x0) * element.width, (y1 - y0) * element.height);
          context.strokeRect(x0 * element.width, y0 * element.height, (x1 - x0) * element.width, (y1 - y0) * element.height);
          context.restore();
        }
        setRendered(true);
        if (scroll.current) scroll.current.scrollTop = validBbox && pageNumber === targetPage ? Math.max(0, bbox[1] * viewport.height - 80) : 0;
      } catch (error: any) {
        if (alive && error?.name !== "RenderingCancelledException") setRenderError(error?.message || "PDF 渲染失败");
      }
    })();
    return () => { alive = false; task?.cancel(); };
  }, [pdf, pageNumber, zoom, width, bboxKey, targetPage]);

  return <div className="pdf-reader">
    <div className="pdf-toolbar" aria-label="PDF 工具栏">
      <button className="icon" title="上一页" aria-label="PDF 上一页" disabled={!pdf || pageNumber <= 1} onClick={() => setPage(pageNumber - 1)}><Icon name="previous" /></button>
      <label><span className="sr-only">PDF 页码</span><input aria-label="PDF 页码" type="number" min="1" max={pdf?.numPages || 1} value={pageNumber} disabled={!pdf} onChange={(event) => setPage(Math.max(1, Math.min(pdf?.numPages || 1, Number(event.target.value) || 1)))} /><span>/ {pdf?.numPages || "—"}</span></label>
      <button className="icon" title="下一页" aria-label="PDF 下一页" disabled={!pdf || pageNumber >= pdf.numPages} onClick={() => setPage(pageNumber + 1)}><Icon name="next" /></button>
      <span className="toolbar-spacer" />
      <button className="icon" title="缩小" aria-label="缩小" disabled={zoom <= .5} onClick={() => setZoom(Math.max(.5, zoom - .25))}><Icon name="minus" /></button>
      <button className="zoom-value" title="适合宽度" aria-label="适合宽度" onClick={() => setZoom(1)}>{Math.round(zoom * 100)}%</button>
      <button className="icon" title="放大" aria-label="放大" disabled={zoom >= 3} onClick={() => setZoom(Math.min(3, zoom + .25))}><Icon name="plus" /></button>
    </div>
    <div className="pdf-scroll" ref={scroll} aria-busy={!rendered && !loadError && !renderError}>
      {(loadError || renderError) && <p role="alert" className="error">{loadError || renderError}</p>}
      {!pdf && !loadError && <p className="muted">正在加载 PDF…</p>}
      {bbox && !validBbox && <p className="muted">原文坐标无效，按页定位。</p>}
      <canvas ref={canvas} aria-label={`PDF 第 ${pageNumber} 页`} style={{ visibility: rendered ? "visible" : "hidden" }} />
    </div>
  </div>;
}
