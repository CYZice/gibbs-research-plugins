import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowLeftOutlined,
  ArrowRightOutlined,
  BookOutlined,
  CheckCircleFilled,
  FileSearchOutlined,
  FolderAddOutlined,
  FolderOpenOutlined,
  LinkOutlined,
  LoadingOutlined,
  MoreOutlined,
  PlusOutlined,
  StarFilled,
  StarOutlined,
} from '@ant-design/icons';
import { Dropdown, Input, Modal, Select } from 'antd';
import {
  addLiteratureCollectionItem,
  createLiteratureCollection,
  listLiteratureCollections,
  listLiteratureSavedPapers,
  patchLiteratureCollection,
  removeLiteratureCollectionItem,
  saveLiteraturePaper,
  setLiteraturePaperFavorite,
  type LiteratureCollection,
  type LiteraturePaperSnapshot,
  type LiteratureSavedPaper,
} from '../../api/literatureLibrary';
import {
  getLiteraturePaper,
  getEvidenceSource,
  getLiteratureStatus,
  listLiteratureUploads,
  retryLiteratureUpload,
  searchLiterature,
  searchGlobalEvidence,
  searchPaperEvidence,
  uploadLiteraturePdf,
  handoffLiteratureToDiagnostic,
  type EvidenceHit,
  type LiteratureHit,
  type LiteratureStatus,
  type LiteratureUpload,
  type PaperSummary,
} from '../../api/literatureWorkspace';
import { getProjects } from '../../api/projects';
import { extractRequestErrorMessage } from '../../utils/requestError';
import {
  parseLiteratureWorkspaceUrl,
  serializeLiteratureWorkspaceUrl,
  type LiteratureAvailabilityFilter,
  type LiteratureLibraryMode,
  type LiteratureSort,
  type LiteratureSearchMode,
} from '../../pages/literatureWorkspaceState';
import LiteraturePdfReader from '../../pages/literature/LiteraturePdfReader';
import ScientificMarkdown from '../../pages/literature/ScientificMarkdown';
import LiteratureDocumentPane from './LiteratureDocumentPane';
import LiteratureEvidencePane from './LiteratureEvidencePane';
import LiteratureLibraryPane from './LiteratureLibraryPane';
import LiteratureUploadQueue from './LiteratureUploadQueue';
import LiteratureWorkbenchHeader from './LiteratureWorkbenchHeader';
import type { LiteratureWorkbenchProps } from './literatureWorkbenchContracts';
import { useLiteratureWorkbench } from './useLiteratureWorkbench';
import '../../pages/LiteratureWorkspace.css';


function authorName(author: Record<string, unknown>): string {
  if (typeof author.name === 'string') return author.name;
  return [author.given, author.family].filter((item) => typeof item === 'string').join(' ');
}

function paperTitle(hit: LiteratureHit): string {
  return hit.metadata.title || hit.local_paper?.title || '未命名论文';
}

type CollectionDialog = {
  mode: 'create' | 'rename';
  collectionId?: number;
} | null;

function paperSnapshot(hit: LiteratureHit): LiteraturePaperSnapshot {
  return {
    identity_key: hit.identity_key,
    local_paper_id: hit.local_paper?.paper_id || null,
    doi: hit.metadata.doi || hit.local_paper?.doi || null,
    title: paperTitle(hit),
    authors: hit.metadata.authors,
    year: hit.metadata.year || hit.local_paper?.year || null,
    venue: hit.metadata.venue || hit.local_paper?.venue || null,
    origins: hit.origins,
    availability_snapshot: hit.availability,
  };
}

function paperSummaryHit(paper: PaperSummary): LiteratureHit {
  return {
    identity_key: paper.doi ? `doi:${paper.doi.toLowerCase()}` : `paper:${paper.paper_id}`,
    metadata: {
      title: paper.title,
      authors: paper.authors || [],
      year: paper.year,
      doi: paper.doi,
      venue: paper.venue,
      abstract: paper.abstract,
      open_access: paper.open_access,
    },
    origins: [{ source: 'local', id: paper.paper_id }],
    local_paper: paper,
    availability: paper.capabilities.can_read_content ? 'local_ready' : 'local_processing',
    score: 0,
    possible_duplicate: false,
  };
}

function availabilityLabel(availability: LiteratureHit['availability']): string {
  if (availability === 'local_ready') return '全文就绪';
  if (availability === 'local_processing') return '处理中';
  if (availability === 'remote_downloadable') return '待入库';
  return '处理失败';
}

function compactUpdatedAt(value: string): string {
  const updatedAt = new Date(value);
  if (Number.isNaN(updatedAt.getTime())) return '时间未知';
  return updatedAt.toLocaleDateString('zh-CN', {
    month: 'numeric',
    day: 'numeric',
  });
}

function locationLabel(location?: EvidenceHit['location']): string {
  if (!location) return '位置待解析';
  const parts = [
    location.page ? `第 ${location.page} 页` : '',
    location.section_title || '',
  ].filter(Boolean);
  return parts.join(' · ') || '已定位原文';
}

function displayEvidenceQuote(quote: string): string {
  return quote.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
}

function statusLabel(status?: LiteratureStatus): string {
  if (!status) return '检查服务';
  if (status.status === 'available') return '本地文献库';
  if (status.status === 'degraded') return '论文服务异常';
  return '服务未启用';
}

function refineSearchItems(
  sourceItems: LiteratureHit[],
  availability: LiteratureAvailabilityFilter,
  sort: LiteratureSort,
): LiteratureHit[] {
  const filtered = availability === 'all'
    ? [...sourceItems]
    : sourceItems.filter((item) => item.availability === availability);
  if (sort === 'relevance') return filtered;
  return filtered.sort((first, second) => {
    const firstYear = first.metadata.year;
    const secondYear = second.metadata.year;
    if (firstYear === undefined && secondYear === undefined) return 0;
    if (firstYear === undefined) return 1;
    if (secondYear === undefined) return -1;
    return sort === 'year_desc' ? secondYear - firstYear : firstYear - secondYear;
  });
}

export default function LiteratureWorkbench({
  mode,
  targetSessionId,
  targetProjectId,
  initialSearch,
  onUrlStateChange,
  onHandoffComplete,
}: LiteratureWorkbenchProps) {
  const {
    searchParams,
    setSearchParams,
    liveLiteratureWorkspaceParams,
  } = useLiteratureWorkbench({ initialSearch, onUrlStateChange });
  const urlKey = searchParams.toString();
  const urlState = useMemo(
    () => parseLiteratureWorkspaceUrl(new URLSearchParams(urlKey)),
    [urlKey],
  );
  const searchRequestKey = [
    urlState.searchMode,
    urlState.query,
    urlState.evidenceQuery,
    urlState.yearFrom || '',
    urlState.yearTo || '',
    urlState.availability,
    urlState.sort,
    urlState.cursor || '',
    urlState.page,
    urlState.libraryMode,
    urlState.collectionId || '',
  ].join('|');
  const [query, setQuery] = useState(
    urlState.searchMode === 'evidence' ? urlState.evidenceQuery : urlState.query,
  );
  const [searchMode, setSearchMode] = useState<LiteratureSearchMode>(urlState.searchMode);
  const [yearFromInput, setYearFromInput] = useState(urlState.yearFrom?.toString() || '');
  const [yearToInput, setYearToInput] = useState(urlState.yearTo?.toString() || '');
  const [availability, setAvailability] = useState<LiteratureAvailabilityFilter>(
    urlState.availability,
  );
  const [sort, setSort] = useState<LiteratureSort>(urlState.sort);
  const [status, setStatus] = useState<LiteratureStatus>();
  const [items, setItems] = useState<LiteratureHit[]>([]);
  const [globalEvidence, setGlobalEvidence] = useState<EvidenceHit[]>([]);
  const [globalSelectedHit, setGlobalSelectedHit] = useState<LiteratureHit>();
  const [rawResultCount, setRawResultCount] = useState(0);
  const [nextCursor, setNextCursor] = useState<string>();
  const [uploads, setUploads] = useState<LiteratureUpload[]>([]);
  const [uploading, setUploading] = useState(false);
  const [libraryMode, setLibraryMode] = useState<LiteratureLibraryMode>(
    urlState.libraryMode,
  );
  const [savedPapers, setSavedPapers] = useState<LiteratureSavedPaper[]>([]);
  const [collections, setCollections] = useState<LiteratureCollection[]>([]);
  const [projects, setProjects] = useState<Awaited<ReturnType<typeof getProjects>>>([]);
  const [selectedCollectionId, setSelectedCollectionId] = useState<number | undefined>(
    urlState.collectionId,
  );
  const [libraryLoading, setLibraryLoading] = useState(false);
  const [libraryReady, setLibraryReady] = useState(false);
  const [libraryBusyKeys, setLibraryBusyKeys] = useState<Set<string>>(new Set());
  const [favoriteOverrides, setFavoriteOverrides] = useState<Record<string, boolean>>({});
  const [collectionDialog, setCollectionDialog] = useState<CollectionDialog>(null);
  const [collectionName, setCollectionName] = useState('');
  const [collectionDescription, setCollectionDescription] = useState('');
  const [collectionProjectId, setCollectionProjectId] = useState<number | null>(null);
  const [collectionDraftPaper, setCollectionDraftPaper] = useState<
    LiteraturePaperSnapshot | undefined
  >();
  const [collectionSubmitting, setCollectionSubmitting] = useState(false);
  const [selectedKey, setSelectedKey] = useState(urlState.paperKey || '');
  const [paper, setPaper] = useState<PaperSummary>();
  const [evidence, setEvidence] = useState<EvidenceHit[]>([]);
  const [selectedEvidenceId, setSelectedEvidenceId] = useState('');
  const [loading, setLoading] = useState(false);
  const [paperLoading, setPaperLoading] = useState(false);
  const [error, setError] = useState('');
  const [evidenceQuery, setEvidenceQuery] = useState(urlState.paperEvidenceQuery);
  const [evidenceSearching, setEvidenceSearching] = useState(false);
  const [handoffBusy, setHandoffBusy] = useState<'current' | 'new' | null>(null);
  const [refreshToken, setRefreshToken] = useState(0);
  const [resolvedSearchKey, setResolvedSearchKey] = useState('');
  const [searchFailed, setSearchFailed] = useState(false);
  const searchSequence = useRef(0);
  const evidenceSearchSequence = useRef(0);
  const globalEvidenceScrollTop = useRef(0);
  const suppressPdfUrlSync = useRef(false);
  const uploadInputRef = useRef<HTMLInputElement>(null);
  const completedUploadIds = useRef<Set<number>>(new Set());

  const activeCollections = useMemo(
    () => collections.filter((collection) => !collection.archived_at),
    [collections],
  );
  const selectedCollection = useMemo(
    () => (
      activeCollections.find((collection) => collection.id === selectedCollectionId)
      || activeCollections[0]
    ),
    [activeCollections, selectedCollectionId],
  );
  const savedByIdentity = useMemo(
    () => new Map(savedPapers.map((saved) => [saved.identity_key, saved])),
    [savedPapers],
  );
  const displayedItems = items;
  const selected = useMemo(
    () => (
      displayedItems.find((item) => item.identity_key === selectedKey)
      || (globalSelectedHit?.identity_key === selectedKey ? globalSelectedHit : undefined)
    ),
    [displayedItems, globalSelectedHit, selectedKey],
  );
  const selectedEvidence = useMemo(
    () => evidence.find((item) => item.evidence_id === selectedEvidenceId),
    [evidence, selectedEvidenceId],
  );
  const selectedEvidenceIndex = selectedEvidence
    ? evidence.findIndex((item) => item.evidence_id === selectedEvidence.evidence_id)
    : -1;
  const globalSelectedEvidenceIndex = selectedEvidenceId
    ? globalEvidence.findIndex((item) => item.evidence_id === selectedEvidenceId)
    : -1;
  const globalEvidenceGroups = useMemo(() => {
    const groups = new Map<string, { paper: EvidenceHit['paper']; items: EvidenceHit[] }>();
    globalEvidence.forEach((item) => {
      const group = groups.get(item.paper.paper_id);
      if (group) group.items.push(item);
      else groups.set(item.paper.paper_id, { paper: item.paper, items: [item] });
    });
    return Array.from(groups.values());
  }, [globalEvidence]);
  const searchScopeLabel = libraryMode === 'favorites'
    ? '我的收藏'
    : libraryMode === 'collection'
      ? (selectedCollection?.name || '当前文献集')
      : '全部本地论文';
  const localPaperId = selected?.local_paper?.paper_id || '';
  const citationPaper = selected ? {
    paper_id: localPaperId || undefined,
    identity_key: selected.identity_key,
    title: paperTitle(selected),
    doi: paper?.doi || selected.metadata.doi,
    year: paper?.year || selected.metadata.year,
    venue: paper?.venue || selected.metadata.venue,
  } : undefined;
  const snapshotLiveStateUnavailable = Boolean(
    libraryMode !== 'search'
    && localPaperId
    && !paperLoading
    && (!paper || status?.status !== 'available'),
  );

  const refreshLibrary = useCallback(async (showLoading = false): Promise<void> => {
    if (showLoading) setLibraryLoading(true);
    try {
      const [savedResult, collectionResult] = await Promise.all([
        listLiteratureSavedPapers(false),
        listLiteratureCollections(true),
      ]);
      setSavedPapers(savedResult);
      setCollections(collectionResult);
    } catch (libraryError) {
      setError(extractRequestErrorMessage(
        libraryError,
        '收藏与文献集加载失败，请稍后重试。',
      ));
    } finally {
      setLibraryReady(true);
      if (showLoading) setLibraryLoading(false);
    }
  }, []);

  const refreshUploads = useCallback(async (): Promise<void> => {
    try {
      const result = await listLiteratureUploads();
      setUploads(result);
      let completedNow = false;
      result.forEach((upload) => {
        if (upload.status !== 'succeeded' || completedUploadIds.current.has(upload.id)) return;
        completedUploadIds.current.add(upload.id);
        completedNow = true;
      });
      if (completedNow) {
        await refreshLibrary();
        setRefreshToken((current) => current + 1);
      }
    } catch {
      // A transient task-status failure must not hide the searchable local library.
    }
  }, [refreshLibrary]);

  useEffect(() => {
    void getLiteratureStatus().then(setStatus).catch(() => {
      setStatus({
        status: 'degraded',
        full_text_available: false,
        metadata_fallback: true,
      });
    });
  }, []);

  useEffect(() => {
    void refreshLibrary(true);
    void refreshUploads();
    void getProjects().then((result) => {
      setProjects(result.filter((project) => !project.is_public_tutorial));
    }).catch(() => {
      setProjects([]);
    });
  }, [refreshLibrary, refreshUploads]);

  useEffect(() => {
    if (!uploads.some((upload) => (
      upload.status === 'queued'
      || upload.status === 'running'
      || upload.status === 'retrying'
    ))) return undefined;
    const timer = window.setInterval(() => {
      void refreshUploads();
    }, 4000);
    return () => window.clearInterval(timer);
  }, [refreshUploads, uploads]);

  useEffect(() => {
    if (!libraryReady || urlState.libraryMode !== 'collection') return;
    const requestedCollection = activeCollections.find(
      (collection) => collection.id === urlState.collectionId,
    );
    if (requestedCollection) {
      setSelectedCollectionId(requestedCollection.id);
      return;
    }
    const fallbackCollection = activeCollections[0];
    if (fallbackCollection) {
      setSelectedCollectionId(fallbackCollection.id);
      setSearchParams(() => {
        const liveParams = liveLiteratureWorkspaceParams();
        const currentState = parseLiteratureWorkspaceUrl(liveParams);
        if (currentState.libraryMode !== 'collection') return liveParams;
        return serializeLiteratureWorkspaceUrl({
          ...currentState,
          collectionId: fallbackCollection.id,
          paperKey: undefined,
          evidenceId: undefined,
          pdfPage: undefined,
        });
      }, { replace: true });
      return;
    }
    setSelectedCollectionId(undefined);
    if (urlState.collectionId) {
      setSearchParams(() => {
        const liveParams = liveLiteratureWorkspaceParams();
        const currentState = parseLiteratureWorkspaceUrl(liveParams);
        if (currentState.libraryMode !== 'collection') return liveParams;
        return serializeLiteratureWorkspaceUrl({
          ...currentState,
          collectionId: undefined,
          paperKey: undefined,
          evidenceId: undefined,
          pdfPage: undefined,
        });
      }, { replace: true });
    }
  }, [
    activeCollections,
    libraryReady,
    urlState.collectionId,
    urlState.libraryMode,
  ]);

  useEffect(() => {
    setSearchMode(urlState.searchMode);
    setQuery(urlState.searchMode === 'evidence' ? urlState.evidenceQuery : urlState.query);
    setEvidenceQuery(urlState.paperEvidenceQuery);
    setYearFromInput(urlState.yearFrom?.toString() || '');
    setYearToInput(urlState.yearTo?.toString() || '');
    setAvailability(urlState.availability);
    setSort(urlState.sort);
    setLibraryMode(urlState.libraryMode);
    setSelectedCollectionId(urlState.collectionId);
  }, [urlKey]);

  useEffect(() => {
    const normalized = (
      urlState.searchMode === 'evidence' ? urlState.evidenceQuery : urlState.query
    ).trim();
    setSelectedEvidenceId('');
    setGlobalSelectedHit(undefined);
    setResolvedSearchKey('');
    setSearchFailed(false);
    if (normalized.length === 1 || (
      urlState.searchMode === 'evidence' && normalized.length < 2
    )) {
      setItems([]);
      setGlobalEvidence([]);
      setRawResultCount(0);
      setNextCursor(undefined);
      setSelectedKey('');
      setResolvedSearchKey(searchRequestKey);
      return undefined;
    }

    const controller = new AbortController();
    const sequence = searchSequence.current + 1;
    searchSequence.current = sequence;
    setLoading(true);
    setError('');
    setItems([]);
    setGlobalEvidence([]);
    setRawResultCount(0);
    setNextCursor(undefined);
    setSelectedKey('');
    const libraryScope = urlState.libraryMode === 'search' ? 'all' : urlState.libraryMode;
    const request = urlState.searchMode === 'evidence'
      ? searchGlobalEvidence(normalized, {
        libraryScope,
        collectionId: urlState.collectionId,
        page: urlState.page,
        pageSize: 50,
        signal: controller.signal,
      }).then((result) => {
        if (searchSequence.current !== sequence) return;
        setGlobalEvidence(result);
        setRawResultCount(result.length);
        setNextCursor(undefined);
      })
      : searchLiterature({
        query: normalized,
        yearFrom: urlState.yearFrom,
        yearTo: urlState.yearTo,
        processingStatus: urlState.availability === 'local_ready'
          ? 'ready'
          : urlState.availability === 'local_processing'
            ? 'processing'
            : urlState.availability === 'metadata_only'
              ? 'failed'
              : 'all',
        sort: urlState.sort,
        page: urlState.page,
        pageSize: 30,
        libraryScope,
        collectionId: urlState.collectionId,
        signal: controller.signal,
      }).then((result) => {
      if (searchSequence.current !== sequence) return;
      const visibleItems = refineSearchItems(
        result.items,
        urlState.availability,
        urlState.sort,
      );
      setItems(visibleItems);
      setRawResultCount(result.items.length);
      setNextCursor(result.next_cursor || undefined);
    });
    void request.catch((searchError) => {
      if (controller.signal.aborted || searchSequence.current !== sequence) return;
      setSearchFailed(true);
      setItems([]);
      setRawResultCount(0);
      setNextCursor(undefined);
      setSelectedKey('');
      setError(extractRequestErrorMessage(
        searchError,
        urlState.searchMode === 'evidence'
          ? '跨论文 Evidence 检索失败，请稍后重试。'
          : '本地论文检索失败，请稍后重试。',
      ));
    }).finally(() => {
      if (searchSequence.current === sequence) {
        setLoading(false);
        setResolvedSearchKey(searchRequestKey);
      }
    });

    return () => controller.abort();
  }, [searchRequestKey, refreshToken]);

  useEffect(() => {
    if (libraryMode !== urlState.libraryMode) return;
    if (resolvedSearchKey !== searchRequestKey) return;
    if (urlState.searchMode === 'evidence') return;
    const requested = urlState.paperKey
      ? displayedItems.find((item) => item.identity_key === urlState.paperKey)
      : undefined;
    if (requested) {
      setSelectedKey(requested.identity_key);
      return;
    }
    const current = displayedItems.find((item) => item.identity_key === selectedKey);
    setSelectedKey(current?.identity_key || '');
    if (urlState.paperKey) {
      setSearchParams(() => {
        const liveParams = liveLiteratureWorkspaceParams();
        const currentState = parseLiteratureWorkspaceUrl(liveParams);
        if (
          currentState.libraryMode !== libraryMode
          || currentState.paperKey !== urlState.paperKey
        ) return liveParams;
        return serializeLiteratureWorkspaceUrl({
          ...currentState,
          paperKey: undefined,
          evidenceId: undefined,
          pdfPage: undefined,
        });
      }, { replace: true });
    }
  }, [
    displayedItems,
    libraryMode,
    resolvedSearchKey,
    searchRequestKey,
    selectedKey,
    urlState.libraryMode,
    urlState.paperKey,
    urlState.searchMode,
  ]);

  useEffect(() => {
    if (
      urlState.searchMode !== 'evidence'
      || !urlState.paperKey
      || globalSelectedHit?.identity_key === urlState.paperKey
    ) return undefined;
    const matchingEvidence = globalEvidence.find((item) => {
      const identityKey = item.paper.doi
        ? `doi:${item.paper.doi.toLowerCase()}`
        : `paper:${item.paper.paper_id}`;
      return identityKey === urlState.paperKey;
    });
    if (!matchingEvidence) return undefined;
    let active = true;
    void getLiteraturePaper(matchingEvidence.paper.paper_id).then((summary) => {
      if (!active || suppressPdfUrlSync.current) return;
      const hit = paperSummaryHit(summary);
      setGlobalSelectedHit(hit);
      setSelectedKey(hit.identity_key);
      if (urlState.evidenceId) {
        const selectedHit = globalEvidence.find(
          (item) => item.evidence_id === urlState.evidenceId,
        );
        if (selectedHit) {
          setEvidence([selectedHit]);
          setSelectedEvidenceId(selectedHit.evidence_id);
        }
      }
    }).catch(() => {
      // The search result remains usable even if a stale deep link cannot be restored.
    });
    return () => {
      active = false;
    };
  }, [
    globalEvidence,
    globalSelectedHit,
    urlState.evidenceId,
    urlState.paperKey,
    urlState.searchMode,
  ]);

  const submitSearch = (): void => {
    const normalized = query.trim();
    const yearFrom = yearFromInput ? Number(yearFromInput) : undefined;
    const yearTo = yearToInput ? Number(yearToInput) : undefined;
    if (normalized.length === 1 || (searchMode === 'evidence' && normalized.length < 2)) {
      setError('请输入至少 2 个字符后检索。');
      return;
    }
    if (
      (yearFrom !== undefined && (!Number.isInteger(yearFrom) || yearFrom < 1800 || yearFrom > 2200))
      || (yearTo !== undefined && (!Number.isInteger(yearTo) || yearTo < 1800 || yearTo > 2200))
      || (yearFrom !== undefined && yearTo !== undefined && yearFrom > yearTo)
    ) {
      setError('年份范围应在 1800–2200 之间，且起始年份不能晚于结束年份。');
      return;
    }
    const nextParams = serializeLiteratureWorkspaceUrl({
      ...urlState,
      searchMode,
      query: searchMode === 'paper' ? normalized : urlState.query,
      evidenceQuery: searchMode === 'evidence' ? normalized : urlState.evidenceQuery,
      yearFrom,
      yearTo,
      openAccessOnly: false,
      sources: ['local'],
      availability,
      sort,
      cursor: undefined,
      cursorTrail: [],
      page: 1,
      paperKey: undefined,
      evidenceId: undefined,
      pdfPage: undefined,
    });
    if (nextParams.toString() === urlKey) {
      setRefreshToken((current) => current + 1);
    } else {
      setSearchParams(nextParams);
    }
  };

  const changeSearchMode = (mode: LiteratureSearchMode): void => {
    setSearchMode(mode);
    setQuery(mode === 'evidence' ? urlState.evidenceQuery : urlState.query);
    setSelectedKey('');
    setSearchParams(serializeLiteratureWorkspaceUrl({
      ...urlState,
      searchMode: mode,
      paperKey: undefined,
      evidenceId: undefined,
      pdfPage: undefined,
    }));
  };

  const selectLibraryMode = (mode: LiteratureLibraryMode): void => {
    const collectionId = mode === 'collection'
      ? selectedCollection?.id
      : undefined;
    setLibraryMode(mode);
    setSelectedCollectionId(collectionId);
    setSelectedKey('');
    setSearchParams(serializeLiteratureWorkspaceUrl({
      ...urlState,
      libraryMode: mode,
      collectionId,
      paperKey: undefined,
      evidenceId: undefined,
      pdfPage: undefined,
    }));
  };

  const selectPaper = (identityKey: string): void => {
    suppressPdfUrlSync.current = false;
    setGlobalSelectedHit(undefined);
    setSelectedKey(identityKey);
    setSearchParams(serializeLiteratureWorkspaceUrl({
      ...urlState,
      paperKey: identityKey,
      evidenceId: undefined,
      pdfPage: 1,
      view: 'pdf',
      paperEvidenceQuery: '',
    }));
  };

  const openGlobalEvidence = async (
    item: EvidenceHit,
    locateEvidence: boolean,
  ): Promise<void> => {
    suppressPdfUrlSync.current = false;
    setPaperLoading(true);
    setError('');
    try {
      const summary = await getLiteraturePaper(item.paper.paper_id);
      const hit = paperSummaryHit(summary);
      setGlobalSelectedHit(hit);
      setSelectedKey(hit.identity_key);
      setEvidence(locateEvidence ? [item] : []);
      setSelectedEvidenceId(locateEvidence ? item.evidence_id : '');
      setSearchParams(serializeLiteratureWorkspaceUrl({
        ...urlState,
        paperKey: hit.identity_key,
        evidenceId: locateEvidence ? item.evidence_id : undefined,
        pdfPage: locateEvidence ? item.location?.page : 1,
        view: 'pdf',
        paperEvidenceQuery: '',
      }));
    } catch (openError) {
      setError(extractRequestErrorMessage(openError, '论文原文打开失败。'));
    } finally {
      setPaperLoading(false);
    }
  };

  const returnToGlobalEvidenceResults = (): void => {
    suppressPdfUrlSync.current = true;
    setGlobalSelectedHit(undefined);
    setSelectedKey('');
    setEvidence([]);
    setSelectedEvidenceId('');
    setSearchParams(() => {
      const currentState = parseLiteratureWorkspaceUrl(liveLiteratureWorkspaceParams());
      return serializeLiteratureWorkspaceUrl({
        ...currentState,
        paperKey: undefined,
        evidenceId: undefined,
        pdfPage: undefined,
        paperEvidenceQuery: '',
      });
    });
  };

  const openAdjacentGlobalEvidence = (offset: -1 | 1): void => {
    if (globalSelectedEvidenceIndex < 0) return;
    const target = globalEvidence[globalSelectedEvidenceIndex + offset];
    if (target) void openGlobalEvidence(target, true);
  };

  const selectEvidence = (evidenceId: string, replace = false): void => {
    setSelectedEvidenceId(evidenceId);
    setSearchParams(serializeLiteratureWorkspaceUrl({
      ...urlState,
      paperKey: selectedKey || undefined,
      evidenceId,
      pdfPage: undefined,
    }), { replace });
  };

  const setCenterView = (view: 'pdf' | 'summary'): void => {
    setSearchParams(serializeLiteratureWorkspaceUrl({
      ...urlState,
      view,
    }), { replace: true });
  };

  const setPdfPage = (pdfPage: number): void => {
    if (suppressPdfUrlSync.current) return;
    setSearchParams(() => {
      const currentState = parseLiteratureWorkspaceUrl(
        liveLiteratureWorkspaceParams(),
      );
      return serializeLiteratureWorkspaceUrl({
        ...currentState,
        paperKey: selectedKey || undefined,
        pdfPage,
      });
    }, { replace: true });
  };

  const setPdfZoom = (pdfZoom: number): void => {
    if (suppressPdfUrlSync.current) return;
    setSearchParams(() => {
      const currentState = parseLiteratureWorkspaceUrl(
        liveLiteratureWorkspaceParams(),
      );
      return serializeLiteratureWorkspaceUrl({
        ...currentState,
        paperKey: selectedKey || undefined,
        pdfZoom,
      });
    }, { replace: true });
  };

  const goToNextPage = (): void => {
    if (!nextCursor || urlState.page >= 21) return;
    setSearchParams(serializeLiteratureWorkspaceUrl({
      ...urlState,
      cursor: nextCursor,
      cursorTrail: [...urlState.cursorTrail, urlState.cursor || ''],
      page: urlState.page + 1,
      paperKey: undefined,
      evidenceId: undefined,
      pdfPage: undefined,
    }));
  };

  const goToPreviousPage = (): void => {
    if (urlState.page <= 1) return;
    const cursorTrail = [...urlState.cursorTrail];
    const previousCursor = cursorTrail.pop() || undefined;
    setSearchParams(serializeLiteratureWorkspaceUrl({
      ...urlState,
      cursor: previousCursor,
      cursorTrail,
      page: Math.max(1, urlState.page - 1),
      paperKey: undefined,
      evidenceId: undefined,
      pdfPage: undefined,
    }));
  };

  const uploadPdf = async (file: File): Promise<void> => {
    if (file.type !== 'application/pdf' && !file.name.toLowerCase().endsWith('.pdf')) {
      setError('请选择 PDF 文件。');
      return;
    }
    if (file.size <= 0 || file.size > 50 * 1024 * 1024) {
      setError('PDF 大小应在 50 MiB 以内。');
      return;
    }
    setUploading(true);
    setError('');
    try {
      const upload = await uploadLiteraturePdf(file, {
        projectId: selectedCollection?.project_id || undefined,
        collectionId: libraryMode === 'collection'
          ? selectedCollection?.id
          : undefined,
      });
      setUploads((current) => [
        upload,
        ...current.filter((candidate) => candidate.id !== upload.id),
      ]);
    } catch (uploadError) {
      setError(extractRequestErrorMessage(
        uploadError,
        'PDF 上传失败，请检查文件后重试。',
      ));
    } finally {
      setUploading(false);
      if (uploadInputRef.current) uploadInputRef.current.value = '';
    }
  };

  const retryUpload = async (uploadId: number): Promise<void> => {
    try {
      const retried = await retryLiteratureUpload(uploadId);
      setUploads((current) => current.map(
        (upload) => (upload.id === retried.id ? retried : upload),
      ));
    } catch (retryError) {
      setError(extractRequestErrorMessage(
        retryError,
        '该 PDF 暂时无法重试。',
      ));
    }
  };

  const setLibraryActionBusy = (key: string, busy: boolean): void => {
    setLibraryBusyKeys((current) => {
      const next = new Set(current);
      if (busy) next.add(key);
      else next.delete(key);
      return next;
    });
  };

  const mergeSavedPaper = (
    savedPaper: LiteratureSavedPaper,
    deleted = false,
  ): void => {
    setSavedPapers((current) => {
      const withoutCurrent = current.filter((item) => item.id !== savedPaper.id);
      return deleted ? withoutCurrent : [savedPaper, ...withoutCurrent];
    });
    setCollections((current) => current.map((collection) => ({
      ...collection,
      items: deleted
        ? collection.items.filter((item) => item.id !== savedPaper.id)
        : collection.items.map((item) => (
          item.id === savedPaper.id ? savedPaper : item
        )),
    })));
  };

  const favoriteState = (item: LiteratureHit): boolean => (
    favoriteOverrides[item.identity_key]
    ?? savedByIdentity.get(item.identity_key)?.is_favorite
    ?? false
  );

  const toggleFavorite = async (item: LiteratureHit): Promise<void> => {
    const currentSaved = savedByIdentity.get(item.identity_key);
    const nextFavorite = !favoriteState(item);
    const busyKey = `favorite:${item.identity_key}`;
    setFavoriteOverrides((current) => ({
      ...current,
      [item.identity_key]: nextFavorite,
    }));
    setLibraryActionBusy(busyKey, true);
    try {
      if (currentSaved) {
        const mutation = await setLiteraturePaperFavorite(
          currentSaved.id,
          nextFavorite,
        );
        mergeSavedPaper(mutation.saved_paper, mutation.deleted);
      } else {
        const savedPaper = await saveLiteraturePaper(
          paperSnapshot(item),
          nextFavorite,
        );
        mergeSavedPaper(savedPaper);
      }
    } catch (favoriteError) {
      setError(extractRequestErrorMessage(
        favoriteError,
        nextFavorite ? '收藏失败，状态已回滚。' : '取消收藏失败，状态已回滚。',
      ));
    } finally {
      setFavoriteOverrides((current) => {
        const next = { ...current };
        delete next[item.identity_key];
        return next;
      });
      setLibraryActionBusy(busyKey, false);
    }
  };

  const addToCollection = async (
    collectionId: number,
    item: LiteratureHit,
  ): Promise<void> => {
    const busyKey = `collection:${item.identity_key}`;
    setLibraryActionBusy(busyKey, true);
    try {
      await addLiteratureCollectionItem(collectionId, paperSnapshot(item));
      await refreshLibrary();
      setLibraryMode('collection');
      setSelectedCollectionId(collectionId);
      setSelectedKey(item.identity_key);
      setSearchParams(serializeLiteratureWorkspaceUrl({
        ...urlState,
        libraryMode: 'collection',
        collectionId,
        paperKey: item.identity_key,
        evidenceId: undefined,
        pdfPage: undefined,
      }));
    } catch (collectionError) {
      setError(extractRequestErrorMessage(
        collectionError,
        '加入文献集失败，请重试。',
      ));
    } finally {
      setLibraryActionBusy(busyKey, false);
    }
  };

  const removeFromCollection = async (
    collectionId: number,
    item: LiteratureHit,
  ): Promise<void> => {
    const savedPaper = savedByIdentity.get(item.identity_key)
      || selectedCollection?.items.find(
        (candidate) => candidate.identity_key === item.identity_key,
      );
    if (!savedPaper) return;
    const busyKey = `collection:${item.identity_key}`;
    setLibraryActionBusy(busyKey, true);
    try {
      await removeLiteratureCollectionItem(collectionId, savedPaper.id);
      await refreshLibrary();
    } catch (collectionError) {
      setError(extractRequestErrorMessage(
        collectionError,
        '移出文献集失败，请重试。',
      ));
    } finally {
      setLibraryActionBusy(busyKey, false);
    }
  };

  const openCreateCollection = (item?: LiteratureHit): void => {
    setCollectionName('');
    setCollectionDescription('');
    setCollectionProjectId(null);
    setCollectionDraftPaper(item ? paperSnapshot(item) : undefined);
    setCollectionDialog({ mode: 'create' });
  };

  const openRenameCollection = (collection: LiteratureCollection): void => {
    setCollectionName(collection.name);
    setCollectionDescription(collection.description || '');
    setCollectionProjectId(collection.project_id || null);
    setCollectionDraftPaper(undefined);
    setCollectionDialog({ mode: 'rename', collectionId: collection.id });
  };

  const closeCollectionDialog = (): void => {
    if (collectionSubmitting) return;
    setCollectionDialog(null);
    setCollectionDraftPaper(undefined);
  };

  const submitCollectionDialog = async (): Promise<void> => {
    const normalizedName = collectionName.trim();
    if (!collectionDialog || !normalizedName) return;
    setCollectionSubmitting(true);
    try {
      if (collectionDialog.mode === 'create') {
        const collection = await createLiteratureCollection({
          name: normalizedName,
          description: collectionDescription.trim() || null,
          project_id: collectionProjectId,
        });
        setCollections((current) => [collection, ...current]);
        setSelectedCollectionId(collection.id);
        setLibraryMode('collection');
        setSearchParams(() => {
          const currentState = parseLiteratureWorkspaceUrl(
            liveLiteratureWorkspaceParams(),
          );
          return serializeLiteratureWorkspaceUrl({
            ...currentState,
            libraryMode: 'collection',
            collectionId: collection.id,
            paperKey: collectionDraftPaper?.identity_key,
            evidenceId: undefined,
            pdfPage: undefined,
          });
        });
        if (collectionDraftPaper) {
          await addLiteratureCollectionItem(collection.id, collectionDraftPaper);
          await refreshLibrary();
          setSelectedKey(collectionDraftPaper.identity_key);
        }
      } else if (collectionDialog.collectionId) {
        const collection = await patchLiteratureCollection(
          collectionDialog.collectionId,
          {
            name: normalizedName,
            description: collectionDescription.trim() || null,
          },
        );
        setCollections((current) => current.map((item) => (
          item.id === collection.id ? collection : item
        )));
      }
      setCollectionDialog(null);
      setCollectionDraftPaper(undefined);
    } catch (collectionError) {
      setError(extractRequestErrorMessage(
        collectionError,
        collectionDialog.mode === 'create'
          ? '创建文献集失败，请重试。'
          : '更新文献集失败，请重试。',
      ));
    } finally {
      setCollectionSubmitting(false);
    }
  };

  const setCollectionArchived = async (
    collection: LiteratureCollection,
    archived: boolean,
  ): Promise<void> => {
    const busyKey = `collection-record:${collection.id}`;
    setLibraryActionBusy(busyKey, true);
    try {
      const updated = await patchLiteratureCollection(
        collection.id,
        { archived },
      );
      setCollections((current) => current.map((item) => (
        item.id === updated.id ? updated : item
      )));
    } catch (collectionError) {
      setError(extractRequestErrorMessage(
        collectionError,
        archived ? '归档文献集失败。' : '恢复文献集失败。',
      ));
    } finally {
      setLibraryActionBusy(busyKey, false);
    }
  };

  useEffect(() => {
    let active = true;
    setPaper(undefined);
    setEvidence((current) => current.filter((item) => item.paper.paper_id === localPaperId));
    setSelectedEvidenceId((current) => (
      evidence.some((item) => (
        item.paper.paper_id === localPaperId && item.evidence_id === current
      )) ? current : ''
    ));
    setEvidenceSearching(false);
    evidenceSearchSequence.current += 1;
    if (!localPaperId) return undefined;
    if (selected?.local_paper) {
      setPaper(selected.local_paper);
      setPaperLoading(false);
      return undefined;
    }
    setPaperLoading(true);
    void getLiteraturePaper(localPaperId).then((paperResult) => {
      if (!active) return;
      setPaper(paperResult);
    }).catch((paperError) => {
      if (!active) return;
      setError(extractRequestErrorMessage(paperError, '论文信息加载失败。'));
    }).finally(() => {
      if (active) setPaperLoading(false);
    });
    return () => {
      active = false;
    };
  }, [localPaperId]);

  useEffect(() => {
    const evidenceId = urlState.evidenceId;
    if (!localPaperId || !evidenceId) return undefined;
    const restoredEvidence = evidence.find((item) => item.evidence_id === evidenceId);
    if (restoredEvidence) {
      setSelectedEvidenceId(evidenceId);
      return undefined;
    }
    let active = true;
    void getEvidenceSource(evidenceId).then((source) => {
      if (!active || source.citation.paper.paper_id !== localPaperId) return;
      const restored: EvidenceHit = {
        evidence_id: evidenceId,
        representation: { representation_id: evidenceId, type: 'text' },
        paper: source.citation.paper,
        content: {
          quote: source.citation.quote,
          snippet: source.citation.quote,
        },
        location: source.citation.location,
        modality: 'text',
      };
      setEvidence([restored]);
      setSelectedEvidenceId(evidenceId);
    }).catch(() => {
      if (active) setSelectedEvidenceId('');
    });
    return () => {
      active = false;
    };
  }, [evidence, localPaperId, urlState.evidenceId]);

  const searchWithinPaper = async (): Promise<void> => {
    const normalized = evidenceQuery.trim();
    if (!localPaperId || normalized.length < 2) return;
    const sequence = evidenceSearchSequence.current + 1;
    evidenceSearchSequence.current = sequence;
    setEvidenceSearching(true);
    setError('');
    try {
      const result = await searchPaperEvidence(normalized, [localPaperId]);
      if (evidenceSearchSequence.current !== sequence) return;
      setEvidence(result);
      setSelectedEvidenceId('');
      setSearchParams(() => {
        const liveParams = liveLiteratureWorkspaceParams();
        const currentState = parseLiteratureWorkspaceUrl(liveParams);
        if (currentState.paperKey !== selectedKey) return liveParams;
        return serializeLiteratureWorkspaceUrl({
          ...currentState,
          evidenceId: undefined,
          pdfPage: undefined,
          paperEvidenceQuery: normalized,
        });
      }, { replace: true });
    } catch (searchError) {
      if (evidenceSearchSequence.current !== sequence) return;
      setError(extractRequestErrorMessage(searchError, '论文内 Evidence 检索失败，请稍后重试。'));
    } finally {
      if (evidenceSearchSequence.current === sequence) setEvidenceSearching(false);
    }
  };

  const handoff = async (target: 'current' | 'new'): Promise<void> => {
    if (!selected || !localPaperId) return;
    const evidenceIds = selectedEvidence
      ? [selectedEvidence.evidence_id]
      : evidence.map((item) => item.evidence_id).slice(0, 3);
    const handoffSessionId = target === 'current'
      ? (targetSessionId || urlState.targetSessionId)
      : undefined;
    setHandoffBusy(target);
    try {
      const result = await handoffLiteratureToDiagnostic({
        sessionId: handoffSessionId,
        projectId: target === 'new'
          ? (selectedCollection?.project_id ?? targetProjectId ?? undefined)
          : undefined,
        paperId: localPaperId,
        evidenceIds,
        title: paperTitle(selected),
      });
      onHandoffComplete(result);
    } catch (handoffError) {
      setError(extractRequestErrorMessage(handoffError, '引用交接失败。'));
    } finally {
      setHandoffBusy(null);
    }
  };

  return (
    <div className={`literature-workspace literature-workbench mode-${mode}`}>
      <LiteratureWorkbenchHeader
        searchMode={searchMode}
        query={query}
        searchScopeLabel={searchScopeLabel}
        loading={loading}
        uploading={uploading}
        serviceStatus={statusLabel(status)}
        serviceClassName={status?.status || 'checking'}
        uploadInputRef={uploadInputRef}
        onQueryChange={setQuery}
        onSearchModeChange={changeSearchMode}
        onSubmitSearch={submitSearch}
        onUploadPdf={(file) => void uploadPdf(file)}
        onChooseUpload={() => uploadInputRef.current?.click()}
      />

      {error && (
        <div className="literature-error" role="alert">
          {error}
          <button type="button" onClick={() => setError('')}>关闭</button>
        </div>
      )}

      <div className="literature-canvas">
        <LiteratureLibraryPane>
          <div className="panel-label">
            <span>
              {searchMode === 'evidence' ? '检索范围' : libraryMode === 'search' ? '检索结果' : (
                libraryMode === 'favorites' ? '我的收藏' : '项目文献集'
              )}
            </span>
            <b>
              {searchMode === 'evidence'
                ? 'Evidence 范围'
                : libraryMode === 'search'
                  ? `第 ${urlState.page} 页 · ${items.length}`
                : displayedItems.length}
            </b>
          </div>
          <div className="literature-library-modes" role="tablist" aria-label="文献范围">
            {([
              ['search', '检索'],
              ['favorites', '收藏'],
              ['collection', '文献集'],
            ] as const).map(([mode, label]) => (
              <button
                type="button"
                role="tab"
                key={mode}
                aria-selected={libraryMode === mode}
                className={libraryMode === mode ? 'active' : ''}
                onClick={() => selectLibraryMode(mode)}
              >
                {label}
              </button>
            ))}
          </div>
          <LiteratureUploadQueue
            uploads={uploads}
            onRetry={(uploadId) => void retryUpload(uploadId)}
          />
          {libraryMode === 'search' && searchMode === 'paper' && (
            <>
              <form
                className="literature-filters"
                onSubmit={(event) => {
                  event.preventDefault();
                  submitSearch();
                }}
              >
                <div className="filter-row year-row">
                  <label>
                    <span>年份</span>
                    <input
                      inputMode="numeric"
                      value={yearFromInput}
                      onChange={(event) => setYearFromInput(event.target.value)}
                      placeholder="起始"
                      aria-label="起始年份"
                    />
                  </label>
                  <i>—</i>
                  <label>
                    <span className="sr-only">结束年份</span>
                    <input
                      inputMode="numeric"
                      value={yearToInput}
                      onChange={(event) => setYearToInput(event.target.value)}
                      placeholder="结束"
                      aria-label="结束年份"
                    />
                  </label>
                </div>
                <div className="filter-row">
                  <label className="filter-select">
                    <span>全文状态</span>
                    <select
                      value={availability}
                      onChange={(event) => setAvailability(
                        event.target.value as LiteratureAvailabilityFilter,
                      )}
                    >
                      <option value="all">全部状态</option>
                      <option value="local_ready">全文就绪</option>
                      <option value="local_processing">处理中</option>
                      <option value="metadata_only">处理失败</option>
                    </select>
                  </label>
                  <label className="filter-select">
                    <span>排序</span>
                    <select
                      value={sort}
                      onChange={(event) => setSort(event.target.value as LiteratureSort)}
                    >
                      <option value="relevance">相关度</option>
                      <option value="year_desc">年份：新到旧</option>
                      <option value="year_asc">年份：旧到新</option>
                    </select>
                  </label>
                </div>
                <div className="filter-footer">
                  <span className="local-library-scope">范围：本地论文库</span>
                  <button type="submit" disabled={loading || query.trim().length === 1}>
                    应用筛选
                  </button>
                </div>
              </form>
            </>
          )}
          {libraryMode !== 'search' && (
            <div className="literature-snapshot-notice">
              <span>
                {status?.status === 'available'
                  ? '保存的是元数据快照；打开论文时重新核验全文状态。'
                  : '收藏快照仍可读；全文实时状态暂不可核验。'}
              </span>
            </div>
          )}
          {libraryMode === 'collection' && (
            <div className="literature-collection-directory">
              <div className="collection-directory-heading">
                <span>文献集</span>
                <button
                  type="button"
                  aria-label="新建文献集"
                  onClick={() => openCreateCollection()}
                >
                  <PlusOutlined />
                </button>
              </div>
              {collections.length === 0 ? (
                <button
                  type="button"
                  className="collection-create-empty"
                  onClick={() => openCreateCollection()}
                >
                  <PlusOutlined /> 新建第一个文献集
                </button>
              ) : (
                <div className="collection-directory-list">
                  {collections.map((collection) => (
                    <div
                      key={collection.id}
                      className={[
                        selectedCollection?.id === collection.id ? 'active' : '',
                        collection.archived_at ? 'archived' : '',
                      ].filter(Boolean).join(' ')}
                    >
                      <button
                        type="button"
                        disabled={Boolean(collection.archived_at)}
                        onClick={() => {
                          setSelectedCollectionId(collection.id);
                          setSelectedKey('');
                          setSearchParams(serializeLiteratureWorkspaceUrl({
                            ...urlState,
                            libraryMode: 'collection',
                            collectionId: collection.id,
                            paperKey: undefined,
                            evidenceId: undefined,
                            pdfPage: undefined,
                          }));
                        }}
                      >
                        <FolderOpenOutlined />
                        <span>
                          <strong>{collection.name}</strong>
                          <small>
                            {collection.item_count} 篇 · {compactUpdatedAt(collection.updated_at)}
                            {collection.archived_at ? ' · 已归档' : ''}
                          </small>
                        </span>
                      </button>
                      <Dropdown
                        trigger={['click']}
                        menu={{
                          items: collection.archived_at ? [
                            { key: 'restore', label: '恢复文献集' },
                          ] : [
                            { key: 'rename', label: '重命名与描述' },
                            { key: 'archive', label: '归档文献集' },
                          ],
                          onClick: ({ key }) => {
                            if (key === 'rename') openRenameCollection(collection);
                            else void setCollectionArchived(
                              collection,
                              key === 'archive',
                            );
                          },
                        }}
                      >
                        <button
                          type="button"
                          aria-label={`${collection.name}操作`}
                          disabled={libraryBusyKeys.has(`collection-record:${collection.id}`)}
                        >
                          <MoreOutlined />
                        </button>
                      </Dropdown>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
          {(libraryMode === 'search' ? loading : libraryLoading) ? (
            <div className="literature-loading">
              <LoadingOutlined />
              {libraryMode === 'search' ? '正在检索本地论文库' : '正在加载个人文献库'}
            </div>
          ) : searchFailed ? (
            <div className="literature-empty small literature-empty-error" role="status">
              <FileSearchOutlined />
              <strong>论文记录加载失败</strong>
              <span>这不代表论文或收藏已被删除。请稍后重试，或切换范围后再返回。</span>
            </div>
          ) : displayedItems.length === 0 ? (
            <div className="literature-empty small">
              {libraryMode === 'collection' ? <FolderOpenOutlined /> : <FileSearchOutlined />}
              <strong>
                {searchMode === 'evidence' ? 'Evidence 结果显示在中间' : libraryMode === 'favorites' ? '还没有收藏论文' : (
                  libraryMode === 'collection' ? (
                    selectedCollection ? '当前文献集为空' : '还没有可用文献集'
                  ) : (
                    rawResultCount > 0 ? '当前页没有符合状态的结果' : '本地库中还没有匹配论文'
                  )
                )}
              </strong>
              <span>
                {searchMode === 'evidence'
                  ? `当前检索范围：${searchScopeLabel}。左侧标签用于切换 Evidence 的检索范围。`
                  : libraryMode === 'favorites'
                  ? '在检索结果中点击星标，收藏会保存到当前账户。'
                  : libraryMode === 'collection'
                    ? '可从检索结果把论文加入当前文献集。'
                    : rawResultCount > 0
                      ? '可调整处理状态，或继续查看下一页。'
                      : '可以修改检索词，也可以上传 PDF，解析完成后会自动出现在这里。'}
              </span>
            </div>
          ) : (
            <div className="literature-result-list">
              {displayedItems.map((item) => {
                const title = paperTitle(item);
                const authors = item.metadata.authors.map(authorName).filter(Boolean);
                const savedPaper = savedByIdentity.get(item.identity_key)
                  || selectedCollection?.items.find(
                    (candidate) => candidate.identity_key === item.identity_key,
                  );
                const favorite = favoriteState(item);
                const collectionBusy = libraryBusyKeys.has(
                  `collection:${item.identity_key}`,
                );
                return (
                  <div
                    key={item.identity_key}
                    className={[
                      'literature-result-item',
                      selectedKey === item.identity_key ? 'active' : '',
                    ].filter(Boolean).join(' ')}
                  >
                    <button
                      type="button"
                      className="literature-result-main"
                      onClick={() => selectPaper(item.identity_key)}
                    >
                      <span className={`availability ${item.availability}`}>
                        {availabilityLabel(item.availability)}
                      </span>
                      <ScientificMarkdown inline className="result-title">{title}</ScientificMarkdown>
                      <small>{authors.slice(0, 3).join('，') || '作者未知'}</small>
                      <footer>
                        <span>{item.metadata.year || '年份未知'}</span>
                        {item.metadata.doi && <span>DOI</span>}
                        {item.possible_duplicate && <span>可能重复</span>}
                        {libraryMode !== 'search' && <span>快照</span>}
                      </footer>
                    </button>
                    <div className="literature-result-actions">
                      <button
                        type="button"
                        className={favorite ? 'active' : ''}
                        aria-label={favorite ? `取消收藏 ${title}` : `收藏 ${title}`}
                        disabled={libraryBusyKeys.has(`favorite:${item.identity_key}`)}
                        onClick={() => void toggleFavorite(item)}
                      >
                        {favorite ? <StarFilled /> : <StarOutlined />}
                      </button>
                      {libraryMode === 'collection' && selectedCollection && savedPaper ? (
                        <button
                          type="button"
                          aria-label={`从 ${selectedCollection.name} 移出 ${title}`}
                          disabled={collectionBusy}
                          onClick={() => void removeFromCollection(
                            selectedCollection.id,
                            item,
                          )}
                        >
                          移出
                        </button>
                      ) : (
                        <Dropdown
                          trigger={['click']}
                          menu={{
                            items: [
                              ...activeCollections.map((collection) => ({
                                key: String(collection.id),
                                label: collection.name,
                                disabled: Boolean(
                                  savedPaper?.collection_ids.includes(collection.id),
                                ),
                              })),
                              ...(activeCollections.length > 0 ? [{ type: 'divider' as const }] : []),
                              { key: 'create', label: '新建文献集…' },
                            ],
                            onClick: ({ key }) => {
                              if (key === 'create') openCreateCollection(item);
                              else void addToCollection(Number(key), item);
                            },
                          }}
                        >
                          <button
                            type="button"
                            aria-label={`将 ${title} 加入文献集`}
                            disabled={collectionBusy}
                          >
                            <FolderAddOutlined />
                          </button>
                        </Dropdown>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
          {searchMode === 'paper' && libraryMode === 'search' && !loading && (items.length > 0 || urlState.page > 1) && (
            <nav className="literature-pagination" aria-label="文献检索分页">
              <button
                type="button"
                disabled={urlState.page <= 1}
                onClick={goToPreviousPage}
              >
                上一页
              </button>
              <span>第 {urlState.page} 页</span>
              <button
                type="button"
                disabled={!nextCursor || urlState.page >= 21}
                onClick={goToNextPage}
              >
                下一页
              </button>
            </nav>
          )}
        </LiteratureLibraryPane>

        <LiteratureDocumentPane>
          {searchMode === 'evidence' && !selected ? (
            <section
              className="global-evidence-results"
              ref={(node) => {
                if (node) node.scrollTop = globalEvidenceScrollTop.current;
              }}
              onScroll={(event) => {
                globalEvidenceScrollTop.current = event.currentTarget.scrollTop;
              }}
            >
              <header>
                <div>
                  <span>跨论文 Evidence</span>
                  <h2>{urlState.evidenceQuery
                    ? `“${urlState.evidenceQuery}”`
                    : '输入研究问题或实验条件'}</h2>
                </div>
                <b>{globalEvidence.length} 条 · {globalEvidenceGroups.length} 篇论文</b>
              </header>
              {loading ? (
                <div className="literature-loading"><LoadingOutlined /> 正在检索原文证据</div>
              ) : globalEvidenceGroups.length === 0 ? (
                <div className="literature-empty">
                  <FileSearchOutlined />
                  <h2>当前范围没有匹配 Evidence</h2>
                  <p>尝试方法名、材料名称、数值或实验条件；结果只来自已解析的本地论文。</p>
                </div>
              ) : (
                globalEvidenceGroups.map((group) => (
                  <article className="global-evidence-group" key={group.paper.paper_id}>
                    <button
                      type="button"
                      className="global-evidence-paper"
                      onClick={() => void openGlobalEvidence(group.items[0], false)}
                    >
                      <ScientificMarkdown inline>{group.paper.title || '未命名论文'}</ScientificMarkdown>
                      <span>
                        {group.paper.year || '年份未知'}
                        {group.paper.doi ? ` · ${group.paper.doi}` : ''}
                        {' · '}打开 PDF
                      </span>
                    </button>
                    <div>
                      {group.items.map((item) => (
                        <button
                          type="button"
                          key={item.evidence_id}
                          className="global-evidence-hit"
                          onClick={() => void openGlobalEvidence(item, true)}
                        >
                          <ScientificMarkdown>{displayEvidenceQuote(item.content.quote)}</ScientificMarkdown>
                          <footer>
                            <span>{locationLabel(item.location)}</span>
                            <code>{item.evidence_id}</code>
                          </footer>
                        </button>
                      ))}
                    </div>
                  </article>
                ))
              )}
            </section>
          ) : !selected ? (
            <div className="literature-empty">
              <div className="evidence-orbit" aria-hidden="true">
                <i /><i /><i />
                <BookOutlined />
              </div>
              <h2>证据先于结论</h2>
              <p>检索论文后，在这里阅读摘要、查看 Evidence，并定位原文段落。</p>
            </div>
          ) : (
            <>
              <div className="reader-toolbar">
                <div className="reader-toolbar-leading">
                  {searchMode === 'evidence' && globalEvidence.length > 0 && (
                    <button
                      type="button"
                      className="global-results-return"
                      onClick={returnToGlobalEvidenceResults}
                    >
                      <ArrowLeftOutlined />
                      返回 Evidence 结果
                      <small>{globalEvidence.length} 条</small>
                    </button>
                  )}
                  <div className="view-switcher" role="tablist" aria-label="文献视图">
                    <button
                      className={urlState.view === 'pdf' ? 'active' : ''}
                      type="button"
                      role="tab"
                      aria-selected={urlState.view === 'pdf'}
                      onClick={() => setCenterView('pdf')}
                    >
                      PDF 原文
                    </button>
                    <button
                      className={urlState.view === 'summary' ? 'active' : ''}
                      type="button"
                      role="tab"
                      aria-selected={urlState.view === 'summary'}
                      onClick={() => setCenterView('summary')}
                    >
                      摘要
                    </button>
                  </div>
                </div>
                <div className="reader-toolbar-context">
                  {globalSelectedEvidenceIndex >= 0 && (
                    <nav aria-label="跨论文 Evidence 结果导航">
                      <button
                        type="button"
                        disabled={globalSelectedEvidenceIndex === 0 || paperLoading}
                        onClick={() => openAdjacentGlobalEvidence(-1)}
                      >
                        上一条
                      </button>
                      <span>
                        {globalSelectedEvidenceIndex + 1} / {globalEvidence.length}
                      </span>
                      <button
                        type="button"
                        disabled={
                          globalSelectedEvidenceIndex === globalEvidence.length - 1
                          || paperLoading
                        }
                        onClick={() => openAdjacentGlobalEvidence(1)}
                      >
                        下一条
                      </button>
                    </nav>
                  )}
                  <span>{paperLoading
                    ? '正在载入论文…'
                    : evidence.length > 0
                      ? `${evidence.length} 条论文内检索结果`
                      : 'PDF 阅读不依赖 Evidence 检索'}</span>
                </div>
              </div>

              {urlState.view === 'pdf'
                && localPaperId
                && citationPaper
                && (paper?.capabilities.can_download_pdf
                  || selected.local_paper?.capabilities.can_download_pdf)
                && !paperLoading ? (
                <LiteraturePdfReader
                  key={localPaperId}
                  paperId={localPaperId}
                  citationPaper={citationPaper}
                  evidence={evidence}
                  selectedEvidenceId={selectedEvidence?.evidence_id || ''}
                  initialPage={urlState.pdfPage}
                  initialZoom={urlState.pdfZoom}
                  onSelectEvidence={selectEvidence}
                  onPageChange={setPdfPage}
                  onZoomChange={setPdfZoom}
                />
              ) : (
                <article className="paper-reading">
                  <div className="paper-kicker">
                    {selected.metadata.venue || paper?.venue || 'Research article'}
                    <span>{selected.metadata.year || paper?.year || ''}</span>
                  </div>
                  <ScientificMarkdown className="paper-title">{paperTitle(selected)}</ScientificMarkdown>
                  <div className="paper-authors">
                    {(paper?.authors || selected.metadata.authors)
                      .map(authorName).filter(Boolean).slice(0, 8).join(' · ') || '作者信息暂缺'}
                  </div>
                  <div className="paper-identifiers">
                    {(paper?.doi || selected.metadata.doi) && (
                      <span><LinkOutlined /> {paper?.doi || selected.metadata.doi}</span>
                    )}
                    {selected.metadata.open_access && <span><CheckCircleFilled /> Open access</span>}
                  </div>
                  <section>
                    <h3>摘要</h3>
                    <ScientificMarkdown>
                      {paper?.abstract || selected.metadata.abstract || '当前记录尚未提供摘要。'}
                    </ScientificMarkdown>
                  </section>
                  {localPaperId ? (
                    <section className="evidence-section">
                      <div className="section-heading">
                        <h3>全文 Evidence</h3>
                        <span>点击片段查看位置，并可引用到研讨室</span>
                      </div>
                      {paperLoading ? (
                        <div className="literature-loading"><LoadingOutlined /> 正在核对 Evidence Source</div>
                      ) : evidence.length === 0 ? (
                        <p className="evidence-gap">全文已入库，但当前问题没有命中可引用片段。</p>
                      ) : (
                        <div className="evidence-list">
                          {evidence.map((item, index) => (
                            <button
                              type="button"
                              key={item.evidence_id}
                              className={selectedEvidence?.evidence_id === item.evidence_id ? 'active' : ''}
                              onClick={() => selectEvidence(item.evidence_id)}
                            >
                              <span className="evidence-index">{String(index + 1).padStart(2, '0')}</span>
                              <ScientificMarkdown>{displayEvidenceQuote(item.content.quote)}</ScientificMarkdown>
                              <footer>
                                <span>{locationLabel(item.location)}</span>
                                <code>{item.evidence_id}</code>
                              </footer>
                            </button>
                          ))}
                        </div>
                      )}
                    </section>
                  ) : (
                    <section className="metadata-notice">
                      <strong>这篇文献尚未进入本地证据库</strong>
                      <p>可以先查看元数据，但不能作为已核验全文证据引用。</p>
                    </section>
                  )}
                </article>
              )}
            </>
          )}
        </LiteratureDocumentPane>

        <LiteratureEvidencePane>
          <div className="panel-label">
            <span>论文内 Evidence 检索</span>
            <b>原文</b>
          </div>
          {!selected ? (
            <div className="inspector-empty">选择一篇论文后，可在当前论文的 Evidence 中检索。</div>
          ) : (
            <>
              <div className="paper-state">
                <span className={
                  localPaperId && !snapshotLiveStateUnavailable ? 'ready' : 'metadata'
                } />
                <div>
                  <strong>
                    {snapshotLiveStateUnavailable
                      ? '全文状态暂不可核验'
                      : localPaperId ? 'Evidence 可检索' : '仅元数据'}
                  </strong>
                  <small>
                    {snapshotLiveStateUnavailable
                      ? '当前仅显示 Molyte 保存的元数据快照'
                      : localPaperId ? '结果只来自当前论文原文' : '等待全文导入和解析'}
                  </small>
                </div>
              </div>

              <div className="evidence-search-thread">
                <div className="evidence-search-intro">
                  <FileSearchOutlined />
                  <p>输入方法、数值、实验条件或关键词，返回可定位的原文 Evidence。</p>
                </div>
                {!paperLoading && localPaperId && evidence.length === 0 && (
                  <div className="evidence-search-empty">
                    当前论文未检索到匹配 Evidence。
                  </div>
                )}
                {evidence.length > 0 && (
                  <>
                    <div className="evidence-result-navigation">
                      <span>{selectedEvidenceIndex >= 0
                        ? `已选择 ${selectedEvidenceIndex + 1} / ${evidence.length}`
                        : `${evidence.length} 条结果，选择一条后定位`}</span>
                      <div>
                        <button
                          type="button"
                          disabled={selectedEvidenceIndex <= 0}
                          onClick={() => selectEvidence(
                            evidence[selectedEvidenceIndex - 1].evidence_id,
                          )}
                        >
                          上一条
                        </button>
                        <button
                          type="button"
                          disabled={
                            selectedEvidenceIndex < 0
                            || selectedEvidenceIndex >= evidence.length - 1
                          }
                          onClick={() => selectEvidence(
                            evidence[selectedEvidenceIndex + 1].evidence_id,
                          )}
                        >
                          下一条
                        </button>
                      </div>
                    </div>
                    <div className="inspector-evidence-list" aria-label="Evidence 检索结果">
                      {evidence.map((item) => (
                        <button
                          type="button"
                          key={item.evidence_id}
                          className={selectedEvidence?.evidence_id === item.evidence_id ? 'active' : ''}
                          onClick={() => selectEvidence(item.evidence_id)}
                        >
                          <ScientificMarkdown>{displayEvidenceQuote(item.content.quote)}</ScientificMarkdown>
                          <span>{locationLabel(item.location)}</span>
                          <code>{item.evidence_id}</code>
                        </button>
                      ))}
                    </div>
                  </>
                )}
              </div>

              <div className="evidence-search-input">
                <textarea
                  value={evidenceQuery}
                  onChange={(event) => setEvidenceQuery(event.target.value)}
                  placeholder="输入方法、数值、实验条件或关键词"
                  aria-label="论文内 Evidence 检索词"
                  disabled={!localPaperId || paperLoading || evidenceSearching}
                  rows={3}
                />
                <button
                  type="button"
                  aria-label="检索当前论文 Evidence"
                  disabled={
                    !localPaperId
                    || paperLoading
                    || evidenceQuery.trim().length < 2
                    || evidenceSearching
                  }
                  onClick={() => void searchWithinPaper()}
                >
                  {evidenceSearching ? <LoadingOutlined /> : <ArrowRightOutlined />}
                </button>
              </div>

              <div className="handoff-actions">
                <span>引用到智能研发</span>
                {(targetSessionId || urlState.targetSessionId) && (
                  <button
                    type="button"
                    disabled={!localPaperId || !selectedEvidence || handoffBusy !== null}
                    onClick={() => void handoff('current')}
                  >
                    {handoffBusy === 'current' ? <LoadingOutlined /> : null}
                    引用到当前研讨室
                  </button>
                )}
                <button
                  type="button"
                  disabled={!localPaperId || !selectedEvidence || handoffBusy !== null}
                  onClick={() => void handoff('new')}
                >
                  {handoffBusy === 'new' ? <LoadingOutlined /> : null}
                  引用到新研讨室
                </button>
                <small>服务端会重新核验 Evidence Source；只交接当前证据，不生成结论，也不启动计算。</small>
              </div>
            </>
          )}
        </LiteratureEvidencePane>
      </div>

      <Modal
        rootClassName="literature-collection-modal"
        title={collectionDialog?.mode === 'rename' ? '编辑文献集' : '新建文献集'}
        open={collectionDialog !== null}
        onCancel={closeCollectionDialog}
        onOk={() => void submitCollectionDialog()}
        confirmLoading={collectionSubmitting}
        okButtonProps={{ disabled: collectionName.trim().length === 0 }}
        okText={collectionDialog?.mode === 'rename' ? '保存' : '创建'}
        cancelText="取消"
        width={480}
        destroyOnHidden
      >
        <div className="literature-collection-form">
          {collectionDraftPaper && (
            <div className="collection-form-context">
              创建后将加入：<strong>{collectionDraftPaper.title}</strong>
            </div>
          )}
          <label>
            <span>名称</span>
            <Input
              value={collectionName}
              onChange={(event) => setCollectionName(event.target.value)}
              maxLength={200}
              placeholder="例如：高浓度电解液综述"
              autoFocus
            />
          </label>
          <label>
            <span>描述（可选）</span>
            <Input.TextArea
              value={collectionDescription}
              onChange={(event) => setCollectionDescription(event.target.value)}
              maxLength={4000}
              rows={3}
              placeholder="记录筛选范围或使用目的"
            />
          </label>
          {collectionDialog?.mode === 'create' && (
            <label>
              <span>关联项目（可选）</span>
              <Select
                value={collectionProjectId || 0}
                onChange={(value: number) => setCollectionProjectId(value || null)}
                options={[
                  { value: 0, label: '个人文献集' },
                  ...projects.map((project) => ({
                    value: project.id,
                    label: project.name,
                  })),
                ]}
                style={{ width: '100%' }}
              />
              <small>仅显示当前账户可写的项目；公共教程项目不可关联。</small>
            </label>
          )}
        </div>
      </Modal>
    </div>
  );
}
