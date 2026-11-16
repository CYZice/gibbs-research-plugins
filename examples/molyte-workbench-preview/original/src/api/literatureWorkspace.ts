import apiClient from './client';
import type { ConsultMessage } from './diagnostic';


export interface LiteratureStatus {
  status: 'available' | 'degraded' | 'disabled';
  full_text_available: boolean;
  metadata_fallback: boolean;
  capabilities?: string[];
  reason?: string;
}

export interface PaperLocation {
  page?: number;
  bbox?: number[];
  coordinate_space?: string;
  section_title?: string;
  section_type?: string;
}

export interface PaperRef {
  paper_id: string;
  title?: string;
  year?: number;
  doi?: string;
  venue?: string;
}

export interface PaperSummary extends PaperRef {
  authors?: Array<Record<string, unknown>>;
  abstract?: string;
  keywords?: string[];
  open_access?: boolean;
  capabilities: {
    has_fulltext: boolean;
    can_read_content: boolean;
    can_search_evidence: boolean;
    can_download_pdf: boolean;
    has_assets: boolean;
    ai_ready: boolean;
  };
  processing_status: {
    fulltext: string;
    parse: string;
    evidence: string;
    embedding: string;
    multimodal: string;
  };
}

export interface LiteratureHit {
  identity_key: string;
  metadata: {
    title?: string;
    authors: Array<Record<string, unknown>>;
    year?: number;
    doi?: string;
    venue?: string;
    abstract?: string;
    open_access?: boolean;
  };
  origins: Array<{
    source: string;
    id: string;
  }>;
  local_paper?: PaperSummary;
  availability: 'local_ready' | 'local_processing' | 'remote_downloadable' | 'metadata_only';
  score: number;
  possible_duplicate: boolean;
}

export interface EvidenceHit {
  evidence_id: string;
  representation: {
    representation_id: string;
    type: string;
  };
  paper: PaperRef;
  content: {
    snippet: string;
    quote: string;
    quote_language?: string;
  };
  location?: PaperLocation;
  modality: string;
  block_type?: string;
  score?: number;
}

export interface EvidenceSource {
  citation: {
    evidence_id: string;
    paper: PaperRef;
    quote: string;
    location?: PaperLocation;
  };
  pdf_asset_id?: string | null;
}

export interface EvidenceContentWindow {
  evidence_id: string;
  paper: PaperRef;
  content: {
    text: string;
    evidence_char_start: number;
    evidence_char_end: number;
    window_char_start: number;
    window_char_end: number;
    more_before: boolean;
    more_after: boolean;
  };
  location?: PaperLocation;
}

export interface LiteratureSearchResult {
  search_id: string;
  items: LiteratureHit[];
  next_cursor?: string | null;
  partial: boolean;
  source_status: Array<{
    source: string;
    status: string;
    latency_ms: number;
    cache: string;
    error_code?: string;
  }>;
}

export interface LiteratureSearchOptions {
  query: string;
  yearFrom?: number;
  yearTo?: number;
  processingStatus?: 'all' | 'ready' | 'processing' | 'failed';
  sort?: 'relevance' | 'recent' | 'year_desc' | 'year_asc';
  page?: number;
  pageSize?: number;
  libraryScope?: 'all' | 'favorites' | 'collection';
  collectionId?: number;
  signal?: AbortSignal;
}

export interface LiteratureUpload {
  id: number;
  project_id?: number | null;
  collection_id?: number | null;
  original_filename: string;
  file_size: number;
  optional_title?: string | null;
  local_paper_id?: string | null;
  status: 'queued' | 'running' | 'retrying' | 'succeeded' | 'failed';
  stage?: string | null;
  progress: number;
  error_code?: string | null;
  error_message?: string | null;
  created_at: string;
  updated_at: string;
  finished_at?: string | null;
}

interface PaperSearchResponse {
  items: Array<PaperSummary & {
    score?: number;
  }>;
  pagination: {
    page: number;
    page_size: number;
    total: number;
    total_pages: number;
    has_next: boolean;
    has_prev: boolean;
  };
}

export interface LiteratureHandoff {
  schema_version: 'literature-handoff/0.1';
  kind: 'evidence' | 'research_note';
  paper_id: string;
  evidence_ids: string[];
  title: string;
  note_content?: string;
}

export interface LiteratureDiagnosticHandoff {
  session_id: string;
  created_session: boolean;
  message: ConsultMessage;
}

export async function getLiteratureStatus(): Promise<LiteratureStatus> {
  const response = await apiClient.get<LiteratureStatus>('/literature/status');
  return response.data;
}

export async function searchLiterature(
  options: LiteratureSearchOptions,
): Promise<LiteratureSearchResult> {
  const response = await apiClient.post<PaperSearchResponse>('/literature/search', {
    query: options.query || null,
    year_from: options.yearFrom,
    year_to: options.yearTo,
    processing_status: options.processingStatus ?? 'all',
    sort: options.sort ?? 'relevance',
    page: options.page ?? 1,
    page_size: options.pageSize ?? 30,
    library_scope: options.libraryScope ?? 'all',
    collection_id: options.collectionId,
  }, {
    signal: options.signal,
  });
  return {
    search_id: `local-page-${response.data.pagination.page}`,
    items: response.data.items.map((paper) => {
      const failed = paper.processing_status.parse === 'failed';
      const ready = paper.capabilities.can_read_content;
      return {
        identity_key: paper.doi
          ? `doi:${paper.doi.toLowerCase()}`
          : `paper:${paper.paper_id}`,
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
        availability: failed
          ? 'metadata_only' as const
          : (ready ? 'local_ready' as const : 'local_processing' as const),
        score: paper.score ?? 0,
        possible_duplicate: false,
      };
    }),
    next_cursor: response.data.pagination.has_next
      ? String(response.data.pagination.page + 1)
      : null,
    partial: false,
    source_status: [],
  };
}

export async function uploadLiteraturePdf(
  file: File,
  options: {
    title?: string;
    projectId?: number;
    collectionId?: number;
  } = {},
): Promise<LiteratureUpload> {
  const body = new FormData();
  body.append('file', file);
  if (options.title?.trim()) body.append('title', options.title.trim());
  if (options.projectId) body.append('project_id', String(options.projectId));
  if (options.collectionId) body.append('collection_id', String(options.collectionId));
  const response = await apiClient.post<LiteratureUpload>('/literature/uploads', body, {
    headers: {
      'Content-Type': 'multipart/form-data',
    },
  });
  return response.data;
}

export async function listLiteratureUploads(): Promise<LiteratureUpload[]> {
  const response = await apiClient.get<LiteratureUpload[]>('/literature/uploads');
  return response.data;
}

export async function retryLiteratureUpload(uploadId: number): Promise<LiteratureUpload> {
  const response = await apiClient.post<LiteratureUpload>(
    `/literature/uploads/${uploadId}/retry`,
  );
  return response.data;
}

export async function getLiteraturePaper(paperId: string): Promise<PaperSummary> {
  const response = await apiClient.get<PaperSummary>(
    `/literature/papers/${encodeURIComponent(paperId)}`,
  );
  return response.data;
}

export async function searchPaperEvidence(
  query: string,
  paperIds: string[],
  limit = 12,
): Promise<EvidenceHit[]> {
  const response = await apiClient.post<{ items: EvidenceHit[] }>('/literature/evidence/search', {
    query,
    scope: { paper_ids: paperIds, evidence_ids: [] },
    filters: {},
    top_k: limit,
    page: 1,
    page_size: limit,
  });
  return response.data.items;
}

export async function searchGlobalEvidence(
  query: string,
  options: {
    libraryScope?: 'all' | 'favorites' | 'collection';
    collectionId?: number;
    page?: number;
    pageSize?: number;
    signal?: AbortSignal;
  } = {},
): Promise<EvidenceHit[]> {
  const response = await apiClient.post<{ items: EvidenceHit[] }>(
    '/literature/evidence/global-search',
    {
      query,
      library_scope: options.libraryScope ?? 'all',
      collection_id: options.collectionId,
      page: options.page ?? 1,
      page_size: Math.min(options.pageSize ?? 50, 50),
    },
    { signal: options.signal },
  );
  return response.data.items;
}

export async function getEvidenceSource(evidenceId: string): Promise<EvidenceSource> {
  const response = await apiClient.get<EvidenceSource>(
    `/literature/evidence/${encodeURIComponent(evidenceId)}/source`,
  );
  return response.data;
}

export async function getEvidenceContent(
  evidenceId: string,
): Promise<EvidenceContentWindow> {
  const response = await apiClient.get<EvidenceContentWindow>(
    `/literature/evidence/${encodeURIComponent(evidenceId)}/content`,
    { params: { before: 1200, after: 2400 } },
  );
  return response.data;
}

export function literatureEvidencePdfUrl(evidenceId: string): string {
  return `/api/v1/literature/evidence/${encodeURIComponent(evidenceId)}/pdf`;
}

export function literaturePaperPdfUrl(paperId: string): string {
  return `/api/v1/literature/papers/${encodeURIComponent(paperId)}/pdf`;
}

export async function validateLiteratureHandoff(
  handoff: Omit<LiteratureHandoff, 'schema_version'>,
): Promise<LiteratureHandoff> {
  const response = await apiClient.post<LiteratureHandoff>('/literature/citations', handoff);
  return response.data;
}

export async function handoffLiteratureToDiagnostic(input: {
  sessionId?: string;
  projectId?: number | null;
  paperId: string;
  evidenceIds: string[];
  title?: string;
}): Promise<LiteratureDiagnosticHandoff> {
  const response = await apiClient.post<LiteratureDiagnosticHandoff>(
    '/literature/citations/diagnostic',
    {
      session_id: input.sessionId,
      project_id: input.projectId ?? undefined,
      paper_id: input.paperId,
      evidence_ids: input.evidenceIds,
      title: input.title,
    },
  );
  return response.data;
}
