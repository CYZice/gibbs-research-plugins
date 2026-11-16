import { useEffect, useMemo, useRef, useState } from 'react';
import {
  AimOutlined,
  CloseOutlined,
  CopyOutlined,
  ExportOutlined,
  FilePdfOutlined,
  LinkOutlined,
  LoadingOutlined,
  MinusOutlined,
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import type { PDFDocumentProxy, PDFPageProxy, RenderTask } from 'pdfjs-dist';
import pdfWorkerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import {
  getEvidenceContent,
  getEvidenceSource,
  literaturePaperPdfUrl,
  type EvidenceContentWindow,
  type EvidenceHit,
  type PaperLocation,
} from '../../api/literatureWorkspace';
import { getAccessToken } from '../../api/token';
import { extractRequestErrorMessage } from '../../utils/requestError';
import {
  buildCanonicalEvidenceCitation,
  buildUserSelectionCitation,
  formatLiteratureSource,
  normalizeSelectionText,
  type LiteratureCitation,
  type LiteratureCitationPaper,
} from './literatureCitation';


type PdfjsModule = typeof import('pdfjs-dist');

let pdfjsPromise: Promise<PdfjsModule> | undefined;

function loadPdfjs(): Promise<PdfjsModule> {
  pdfjsPromise ??= import('pdfjs-dist').then((pdfjs) => {
    pdfjs.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;
    return pdfjs;
  });
  return pdfjsPromise;
}

function scrollPdfPage(
  reader: HTMLDivElement | null,
  page: number,
  behavior: ScrollBehavior,
): void {
  const container = reader?.querySelector<HTMLElement>('.literature-pdf-scroll');
  const target = reader?.querySelector<HTMLElement>(
    `#literature-pdf-page-${page}`,
  );
  if (!container || !target) return;
  const containerRect = container.getBoundingClientRect();
  const targetRect = target.getBoundingClientRect();
  container.scrollTo({
    top: container.scrollTop + targetRect.top - containerRect.top,
    behavior,
  });
}

function isNormalizedBbox(location?: PaperLocation): location is PaperLocation & {
  bbox: [number, number, number, number];
  coordinate_space: 'normalized';
} {
  if (
    location?.coordinate_space !== 'normalized'
    || !Array.isArray(location.bbox)
    || location.bbox.length !== 4
  ) return false;
  const [x0, y0, x1, y1] = location.bbox;
  return location.bbox.every((value) => (
    typeof value === 'number' && value >= 0 && value <= 1
  )) && x1 > x0 && y1 > y0;
}

interface PdfPageProps {
  pdf: PDFDocumentProxy;
  pdfjs: PdfjsModule;
  pageNumber: number;
  scale: number;
  location?: PaperLocation;
  active: boolean;
  onVisible: (page: number) => void;
}

function PdfPage({
  pdf,
  pdfjs,
  pageNumber,
  scale,
  location,
  active,
  onVisible,
}: PdfPageProps) {
  const shellRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const textRef = useRef<HTMLDivElement>(null);
  const renderTask = useRef<RenderTask>();
  const textLayer = useRef<InstanceType<PdfjsModule['TextLayer']>>();
  const [visible, setVisible] = useState(active || pageNumber <= 2);
  const [page, setPage] = useState<PDFPageProxy>();
  const [error, setError] = useState('');

  useEffect(() => {
    const element = shellRef.current;
    if (!element || visible) return undefined;
    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) {
        setVisible(true);
        observer.disconnect();
      }
    }, { rootMargin: '900px 0px' });
    observer.observe(element);
    return () => observer.disconnect();
  }, [visible]);

  useEffect(() => {
    const element = shellRef.current;
    if (!element) return undefined;
    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting && entry.intersectionRatio >= 0.35) onVisible(pageNumber);
    }, { threshold: [0.35, 0.7] });
    observer.observe(element);
    return () => observer.disconnect();
  }, [onVisible, pageNumber]);

  useEffect(() => {
    if (!visible) return undefined;
    let alive = true;
    setError('');
    void pdf.getPage(pageNumber).then((result) => {
      if (alive) setPage(result);
    }).catch(() => {
      if (alive) setError('页面加载失败');
    });
    return () => {
      alive = false;
    };
  }, [pdf, pageNumber, visible]);

  useEffect(() => {
    if (!page || !canvasRef.current || !textRef.current) return undefined;
    let alive = true;
    const canvas = canvasRef.current;
    const textContainer = textRef.current;
    const viewport = page.getViewport({ scale });
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    const context = canvas.getContext('2d');
    if (!context) return undefined;

    renderTask.current?.cancel();
    textLayer.current?.cancel();
    canvas.width = Math.floor(viewport.width * ratio);
    canvas.height = Math.floor(viewport.height * ratio);
    canvas.style.width = `${Math.floor(viewport.width)}px`;
    canvas.style.height = `${Math.floor(viewport.height)}px`;
    textContainer.replaceChildren();
    textContainer.style.width = `${Math.floor(viewport.width)}px`;
    textContainer.style.height = `${Math.floor(viewport.height)}px`;
    renderTask.current = page.render({
      canvas,
      canvasContext: context,
      viewport,
      transform: ratio === 1 ? undefined : [ratio, 0, 0, ratio, 0, 0],
    });
    void Promise.all([
      renderTask.current.promise,
      page.getTextContent().then((textContent) => {
        if (!alive) return;
        textLayer.current = new pdfjs.TextLayer({
          textContentSource: textContent,
          container: textContainer,
          viewport,
        });
        return textLayer.current.render();
      }),
    ]).catch((reason: { name?: string }) => {
      if (alive && reason?.name !== 'RenderingCancelledException') {
        setError('页面渲染失败');
      }
    });
    return () => {
      alive = false;
      renderTask.current?.cancel();
      textLayer.current?.cancel();
    };
  }, [page, pdfjs, scale]);

  const highlight = active && location?.page === pageNumber && isNormalizedBbox(location)
    ? {
      left: `${location.bbox[0] * 100}%`,
      top: `${location.bbox[1] * 100}%`,
      width: `${(location.bbox[2] - location.bbox[0]) * 100}%`,
      height: `${(location.bbox[3] - location.bbox[1]) * 100}%`,
    }
    : undefined;

  return (
    <section
      ref={shellRef}
      id={`literature-pdf-page-${pageNumber}`}
      data-page-number={pageNumber}
      className={`literature-pdf-page${active ? ' active' : ''}`}
      aria-label={`PDF 第 ${pageNumber} 页`}
    >
      <span className="pdf-page-number">{pageNumber}</span>
      {!visible || (!page && !error) ? (
        <div className="pdf-page-placeholder"><LoadingOutlined /></div>
      ) : error ? (
        <div className="pdf-page-placeholder error">{error}</div>
      ) : (
        <div className="pdf-page-surface">
          <canvas ref={canvasRef} />
          <div ref={textRef} className="textLayer literature-pdf-text-layer" />
          {highlight && <mark className="literature-bbox-highlight" style={highlight} />}
        </div>
      )}
    </section>
  );
}

interface LiteraturePdfReaderProps {
  paperId: string;
  citationPaper: LiteratureCitationPaper;
  evidence: EvidenceHit[];
  selectedEvidenceId: string;
  initialPage?: number;
  initialZoom?: number;
  onSelectEvidence: (evidenceId: string) => void;
  onPageChange: (page: number) => void;
  onZoomChange: (zoom: number) => void;
}

export default function LiteraturePdfReader({
  paperId,
  citationPaper,
  evidence,
  selectedEvidenceId,
  initialPage,
  initialZoom,
  onSelectEvidence,
  onPageChange,
  onZoomChange,
}: LiteraturePdfReaderProps) {
  const readerRef = useRef<HTMLDivElement>(null);
  const loadingTask = useRef<ReturnType<PdfjsModule['getDocument']>>();
  const pageChangeTimer = useRef<ReturnType<typeof setTimeout>>();
  const lastNavigationKey = useRef('');
  const [pdfjs, setPdfjs] = useState<PdfjsModule>();
  const [pdf, setPdf] = useState<PDFDocumentProxy>();
  const [location, setLocation] = useState<PaperLocation>();
  const [scale, setScale] = useState(initialZoom || 1.1);
  const [currentPage, setCurrentPage] = useState(1);
  const [pageInput, setPageInput] = useState('1');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [fallback, setFallback] = useState<EvidenceContentWindow>();
  const [reloadKey, setReloadKey] = useState(0);
  const [selection, setSelection] = useState<{
    text: string;
    page?: number;
    crossPage: boolean;
  }>();
  const [actionMessage, setActionMessage] = useState('');
  const [citationBusy, setCitationBusy] = useState(false);
  const selected = useMemo(
    () => evidence.find((item) => item.evidence_id === selectedEvidenceId),
    [evidence, selectedEvidenceId],
  );
  const invalidBbox = Boolean(
    location?.bbox && !isNormalizedBbox(location),
  );

  const reportVisiblePage = (page: number): void => {
    setCurrentPage(page);
    setPageInput(String(page));
    if (pageChangeTimer.current) clearTimeout(pageChangeTimer.current);
    pageChangeTimer.current = setTimeout(() => onPageChange(page), 180);
  };

  const clearSelection = (): void => {
    window.getSelection()?.removeAllRanges();
    setSelection(undefined);
  };

  const copyText = async (value: string, successMessage: string): Promise<void> => {
    try {
      await navigator.clipboard.writeText(value);
      setActionMessage(successMessage);
    } catch {
      setActionMessage('复制失败，请允许剪贴板权限后重试。');
    }
  };

  const exportCitation = (citation: LiteratureCitation): void => {
    const blob = new Blob([`${JSON.stringify(citation, null, 2)}\n`], {
      type: 'application/json;charset=utf-8',
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = citation.kind === 'canonical_evidence'
      ? `${citation.evidence_id}.literature-citation.json`
      : `page-${citation.location?.page || 'selection'}.literature-citation.json`;
    link.click();
    URL.revokeObjectURL(url);
    setActionMessage('结构化引用已导出。');
  };

  const loadCanonicalCitation = async (): Promise<LiteratureCitation | undefined> => {
    if (!selected) return undefined;
    setCitationBusy(true);
    setActionMessage('');
    try {
      const source = await getEvidenceSource(selected.evidence_id);
      return buildCanonicalEvidenceCitation(citationPaper, source);
    } catch (reason) {
      setActionMessage(extractRequestErrorMessage(reason, 'Evidence Source 重新核验失败。'));
      return undefined;
    } finally {
      setCitationBusy(false);
    }
  };

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError('');
    setFallback(undefined);
    void loadPdfjs().then((module) => {
      if (!alive) return;
      setPdfjs(module);
      const token = getAccessToken();
      loadingTask.current = module.getDocument({
        url: literaturePaperPdfUrl(paperId),
        httpHeaders: token ? { Authorization: `Bearer ${token}` } : undefined,
        rangeChunkSize: 128 * 1024,
      });
      return loadingTask.current.promise;
    }).then((document) => {
      if (!alive || !document) return;
      setPdf(document);
    }).catch(async (reason) => {
      if (!alive) return;
      setError(extractRequestErrorMessage(reason, 'PDF 原文暂时无法加载。'));
      if (selectedEvidenceId) {
        try {
          const content = await getEvidenceContent(selectedEvidenceId);
          if (alive) setFallback(content);
        } catch {
          // The visible PDF error remains the authoritative failure state.
        }
      }
    }).finally(() => {
      if (alive) setLoading(false);
    });
    return () => {
      alive = false;
      void loadingTask.current?.destroy();
      setPdf(undefined);
    };
  }, [paperId, reloadKey]);

  useEffect(() => {
    setFallback(undefined);
    setLocation(selected?.location);
  }, [selected]);

  useEffect(() => {
    if (!pdf) return;
    const navigationKey = selected?.evidence_id
      ? `evidence:${selected.evidence_id}`
      : `paper:${paperId}`;
    if (lastNavigationKey.current === navigationKey) return;
    lastNavigationKey.current = navigationKey;
    const requestedPage = selected?.location?.page || initialPage || 1;
    const nextPage = Math.max(1, Math.min(pdf.numPages, requestedPage));
    if (!selected?.location?.page && initialPage && nextPage !== initialPage) {
      onPageChange(nextPage);
    }
    window.requestAnimationFrame(() => {
      scrollPdfPage(readerRef.current, nextPage, 'smooth');
    });
  }, [initialPage, onPageChange, paperId, pdf, selected]);

  useEffect(() => {
    const captureSelection = (): void => {
      const root = readerRef.current;
      const browserSelection = window.getSelection();
      if (
        !root
        || !browserSelection
        || browserSelection.rangeCount === 0
        || browserSelection.isCollapsed
      ) {
        setSelection(undefined);
        return;
      }
      const anchorNode = browserSelection.anchorNode;
      const focusNode = browserSelection.focusNode;
      if (!anchorNode || !focusNode || !root.contains(anchorNode) || !root.contains(focusNode)) {
        setSelection(undefined);
        return;
      }
      const anchorElement = anchorNode.nodeType === Node.ELEMENT_NODE
        ? anchorNode as Element
        : anchorNode.parentElement;
      const focusElement = focusNode.nodeType === Node.ELEMENT_NODE
        ? focusNode as Element
        : focusNode.parentElement;
      const anchorLayer = anchorElement?.closest('.literature-pdf-text-layer');
      const focusLayer = focusElement?.closest('.literature-pdf-text-layer');
      if (!anchorLayer || !focusLayer || !root.contains(anchorLayer) || !root.contains(focusLayer)) {
        setSelection(undefined);
        return;
      }
      const text = normalizeSelectionText(browserSelection.toString());
      if (!text) {
        setSelection(undefined);
        return;
      }
      const anchorPage = Number(
        anchorLayer.closest<HTMLElement>('[data-page-number]')?.dataset.pageNumber,
      );
      const focusPage = Number(
        focusLayer.closest<HTMLElement>('[data-page-number]')?.dataset.pageNumber,
      );
      const singlePage = Number.isInteger(anchorPage)
        && anchorPage > 0
        && anchorPage === focusPage;
      setSelection({
        text,
        page: singlePage ? anchorPage : undefined,
        crossPage: !singlePage,
      });
      setActionMessage('');
    };
    document.addEventListener('selectionchange', captureSelection);
    return () => document.removeEventListener('selectionchange', captureSelection);
  }, [paperId, scale]);

  useEffect(() => {
    clearSelection();
    setActionMessage('');
  }, [paperId, scale]);

  useEffect(() => {
    const restoredScale = initialZoom || 1.1;
    if (restoredScale !== scale) setScale(restoredScale);
  }, [initialZoom]);

  useEffect(() => () => {
    if (pageChangeTimer.current) clearTimeout(pageChangeTimer.current);
  }, []);

  const jumpToPage = (page: number): void => {
    if (!pdf) return;
    const nextPage = Math.max(1, Math.min(pdf.numPages, page));
    setCurrentPage(nextPage);
    setPageInput(String(nextPage));
    onPageChange(nextPage);
    scrollPdfPage(readerRef.current, nextPage, 'smooth');
  };

  const changeScale = (nextScale: number): void => {
    const boundedScale = Math.max(
      0.75,
      Math.min(2, Math.round(nextScale * 100) / 100),
    );
    setScale(boundedScale);
    onZoomChange(boundedScale);
  };

  const canonicalActions = (
    <div className="pdf-citation-actions" aria-label="当前 Evidence 引用动作">
      <strong>当前 Evidence</strong>
      <button
        type="button"
        disabled={!selected || citationBusy}
        onClick={() => void loadCanonicalCitation().then((citation) => {
          if (citation?.kind === 'canonical_evidence') {
            void copyText(citation.text, 'Evidence 原文已复制。');
          }
        })}
      >
        <CopyOutlined /> 原文
      </button>
      <button
        type="button"
        disabled={!selected || citationBusy}
        onClick={() => void loadCanonicalCitation().then((citation) => {
          if (citation) void copyText(formatLiteratureSource(citation), '来源已复制。');
        })}
      >
        <CopyOutlined /> 来源
      </button>
      <button
        type="button"
        disabled={!selected}
        onClick={() => void copyText(window.location.href, '稳定深链已复制。')}
      >
        <LinkOutlined /> 深链
      </button>
      <button
        type="button"
        disabled={!selected || citationBusy}
        onClick={() => void loadCanonicalCitation().then((citation) => {
          if (citation) exportCitation(citation);
        })}
      >
        <ExportOutlined /> JSON
      </button>
    </div>
  );

  const selectionActions = selection && (
    <div className="pdf-selection-actions" role="toolbar" aria-label="PDF 选中文本动作">
      <div>
        <strong>{selection.crossPage ? '跨页选中文本' : `第 ${selection.page} 页选中文本`}</strong>
        <span>
          {selection.crossPage
            ? '跨页选择只允许复制纯文本；请重新选择单页内容后再导出。'
            : `${selection.text.length} 字符 · 用户选中文段`}
        </span>
      </div>
      <button
        type="button"
        onClick={() => void copyText(selection.text, '选中文本已复制。')}
      >
        <CopyOutlined /> 复制文本
      </button>
      {!selection.crossPage && (
        <>
          <button
            type="button"
            onClick={() => {
              const citation = buildUserSelectionCitation(
                citationPaper,
                selection.text,
                selection.page,
              );
              void copyText(
                `${citation.text}\n\n${formatLiteratureSource(citation)}`,
                '带来源的选中文段已复制。',
              );
            }}
          >
            <CopyOutlined /> 带来源
          </button>
          <button
            type="button"
            onClick={() => exportCitation(buildUserSelectionCitation(
              citationPaper,
              selection.text,
              selection.page,
            ))}
          >
            <ExportOutlined /> JSON
          </button>
        </>
      )}
      <button type="button" aria-label="清除 PDF 选择" onClick={clearSelection}>
        <CloseOutlined />
      </button>
    </div>
  );

  if (fallback) {
    return (
      <div className="literature-pdf-reader" ref={readerRef}>
        {canonicalActions}
        {actionMessage && <div className="pdf-action-message" role="status">{actionMessage}</div>}
        <div className="literature-pdf-fallback" role="status">
          <FilePdfOutlined />
          <div>
            <strong>PDF 原文暂不可用</strong>
            <p>{error}</p>
            <article>{fallback.content.text}</article>
            <small>
              Evidence Content 原文窗口 · {fallback.location?.page
                ? `第 ${fallback.location.page} 页`
                : '页码不可用'}
            </small>
          </div>
          <button type="button" onClick={() => setReloadKey((value) => value + 1)}>
            <ReloadOutlined /> 重试 PDF
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="literature-pdf-reader" ref={readerRef}>
      <div className="literature-pdf-toolbar">
        <label className="pdf-evidence-picker">
          <span>定位 Evidence</span>
          <select
            value={selected?.evidence_id || ''}
            onChange={(event) => onSelectEvidence(event.target.value)}
          >
            <option value="">未选择 Evidence</option>
            {evidence.map((item, index) => (
              <option key={item.evidence_id} value={item.evidence_id}>
                {String(index + 1).padStart(2, '0')} · {item.location?.page
                  ? `第 ${item.location.page} 页`
                  : '页码待解析'} · {item.location?.section_title || item.content.quote.slice(0, 32)}
              </option>
            ))}
          </select>
        </label>
        <div className="pdf-page-control">
          <AimOutlined />
          <input
            value={pageInput}
            inputMode="numeric"
            aria-label="PDF 页码"
            onChange={(event) => setPageInput(event.target.value)}
            onBlur={() => jumpToPage(Number(pageInput) || currentPage)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') jumpToPage(Number(pageInput) || currentPage);
            }}
          />
          <span>/ {pdf?.numPages || '—'}</span>
        </div>
        <div className="pdf-zoom-control" aria-label="PDF 缩放">
          <button
            type="button"
            aria-label="缩小 PDF"
            disabled={scale <= 0.75}
            onClick={() => changeScale(scale - 0.15)}
          >
            <MinusOutlined />
          </button>
          <span>{Math.round(scale * 100)}%</span>
          <button
            type="button"
            aria-label="放大 PDF"
            disabled={scale >= 2}
            onClick={() => changeScale(scale + 0.15)}
          >
            <PlusOutlined />
          </button>
        </div>
      </div>
      {canonicalActions}
      {selectionActions}
      {actionMessage && <div className="pdf-action-message" role="status">{actionMessage}</div>}

      {invalidBbox && (
        <div className="pdf-contract-notice">
          当前 Evidence 的 bbox 不是合法 normalized 坐标，仅定位到页码，不绘制猜测高亮。
        </div>
      )}
      {error && !loading && !pdf && (
        <div className="pdf-contract-notice error">{error}</div>
      )}
      <div className="literature-pdf-scroll">
        {loading && !pdf ? (
          <div className="literature-pdf-loading">
            <LoadingOutlined />
            <strong>正在读取 PDF 原文</strong>
            <span>文件按需分段加载，不会暴露 Literature KB 凭证。</span>
          </div>
        ) : pdf && pdfjs ? (
          Array.from({ length: pdf.numPages }, (_, index) => index + 1).map((pageNumber) => (
            <PdfPage
              key={`${pageNumber}-${scale}`}
              pdf={pdf}
              pdfjs={pdfjs}
              pageNumber={pageNumber}
              scale={scale}
              location={location}
              active={location?.page === pageNumber}
              onVisible={reportVisiblePage}
            />
          ))
        ) : null}
      </div>
    </div>
  );
}
