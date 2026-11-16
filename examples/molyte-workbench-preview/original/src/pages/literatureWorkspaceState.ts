export const LITERATURE_SOURCES = ['local'] as const;

export type LiteratureSource = typeof LITERATURE_SOURCES[number];
export type LiteratureAvailabilityFilter =
  | 'all'
  | 'local_ready'
  | 'local_processing'
  | 'remote_downloadable'
  | 'metadata_only';
export type LiteratureSort = 'relevance' | 'year_desc' | 'year_asc';
export type LiteratureCenterView = 'pdf' | 'summary';
export type LiteratureLibraryMode = 'search' | 'favorites' | 'collection';
export type LiteratureSearchMode = 'paper' | 'evidence';

export interface LiteratureWorkspaceUrlState {
  query: string;
  evidenceQuery: string;
  paperEvidenceQuery: string;
  searchMode: LiteratureSearchMode;
  yearFrom?: number;
  yearTo?: number;
  openAccessOnly: boolean;
  sources: LiteratureSource[];
  availability: LiteratureAvailabilityFilter;
  sort: LiteratureSort;
  cursor?: string;
  cursorTrail: string[];
  page: number;
  paperKey?: string;
  evidenceId?: string;
  pdfPage?: number;
  pdfZoom?: number;
  collectionId?: number;
  targetSessionId?: string;
  libraryMode: LiteratureLibraryMode;
  view: LiteratureCenterView;
}

const DEFAULT_SOURCES: LiteratureSource[] = ['local'];
const AVAILABILITY_VALUES = new Set<LiteratureAvailabilityFilter>([
  'all',
  'local_ready',
  'local_processing',
  'metadata_only',
]);
const SORT_VALUES = new Set<LiteratureSort>(['relevance', 'year_desc', 'year_asc']);
const VIEW_VALUES = new Set<LiteratureCenterView>(['pdf', 'summary']);
const SEARCH_MODE_VALUES = new Set<LiteratureSearchMode>(['paper', 'evidence']);
const LIBRARY_MODE_VALUES = new Set<LiteratureLibraryMode>([
  'search',
  'favorites',
  'collection',
]);

function boundedValue(value: string | null, maxLength: number): string | undefined {
  const normalized = value?.trim();
  return normalized && normalized.length <= maxLength ? normalized : undefined;
}

function parseYear(value: string | null): number | undefined {
  if (!value || !/^\d{4}$/.test(value)) return undefined;
  const parsed = Number(value);
  return parsed >= 1800 && parsed <= 2200 ? parsed : undefined;
}

function parsePositiveInteger(value: string | null, maximum: number): number | undefined {
  if (!value || !/^\d+$/.test(value)) return undefined;
  const parsed = Number(value);
  return parsed >= 1 && parsed <= maximum ? parsed : undefined;
}

function parseZoom(value: string | null): number | undefined {
  if (!value || !/^\d(?:\.\d{1,2})?$/.test(value)) return undefined;
  const parsed = Number(value);
  return parsed >= 0.75 && parsed <= 2
    ? Math.round(parsed * 100) / 100
    : undefined;
}

function parseSources(value: string | null): LiteratureSource[] {
  if (!value) return DEFAULT_SOURCES;
  const selected = value
    .split(',')
    .filter((item): item is LiteratureSource => (
      LITERATURE_SOURCES.includes(item as LiteratureSource)
    ));
  return selected.length > 0 ? Array.from(new Set(selected)) : DEFAULT_SOURCES;
}

function parseCursorTrail(value: string | null): string[] {
  if (!value || value.length > 12000) return [];
  try {
    const parsed: unknown = JSON.parse(value);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((item): item is string => typeof item === 'string' && item.length <= 512)
      .slice(-20);
  } catch {
    return [];
  }
}

export function parseLiteratureWorkspaceUrl(
  params: URLSearchParams,
): LiteratureWorkspaceUrlState {
  const availabilityValue = params.get('availability') as LiteratureAvailabilityFilter | null;
  const sortValue = params.get('sort') as LiteratureSort | null;
  const viewValue = params.get('view') as LiteratureCenterView | null;
  const libraryValue = params.get('library') as LiteratureLibraryMode | null;
  const searchModeValue = params.get('search_mode') as LiteratureSearchMode | null;
  const libraryMode = libraryValue && LIBRARY_MODE_VALUES.has(libraryValue)
    ? libraryValue
    : 'search';
  const cursor = boundedValue(params.get('cursor'), 512);
  const parsedPage = Number(params.get('page'));

  return {
    query: boundedValue(params.get('q'), 500) || '',
    evidenceQuery: boundedValue(params.get('evidence_q'), 500) || '',
    paperEvidenceQuery: boundedValue(params.get('within_q'), 500) || '',
    searchMode: searchModeValue && SEARCH_MODE_VALUES.has(searchModeValue)
      ? searchModeValue
      : 'paper',
    yearFrom: parseYear(params.get('from')),
    yearTo: parseYear(params.get('to')),
    openAccessOnly: params.get('oa') === '1',
    sources: parseSources(params.get('sources')),
    availability: availabilityValue && AVAILABILITY_VALUES.has(availabilityValue)
      ? availabilityValue
      : 'all',
    sort: sortValue && SORT_VALUES.has(sortValue) ? sortValue : 'relevance',
    cursor,
    cursorTrail: cursor ? parseCursorTrail(params.get('trail')) : [],
    page: cursor && Number.isInteger(parsedPage) && parsedPage > 1
      ? Math.min(parsedPage, 21)
      : 1,
    paperKey: boundedValue(params.get('paper'), 500),
    evidenceId: boundedValue(params.get('evidence'), 500),
    pdfPage: parsePositiveInteger(params.get('pdf_page'), 100000),
    pdfZoom: parseZoom(params.get('zoom')),
    collectionId: libraryMode === 'collection'
      ? parsePositiveInteger(params.get('collection'), 2147483647)
      : undefined,
    targetSessionId: boundedValue(params.get('session'), 64),
    libraryMode,
    view: viewValue && VIEW_VALUES.has(viewValue) ? viewValue : 'pdf',
  };
}

export function serializeLiteratureWorkspaceUrl(
  state: LiteratureWorkspaceUrlState,
): URLSearchParams {
  const params = new URLSearchParams();
  if (state.query) params.set('q', state.query);
  if (state.evidenceQuery) params.set('evidence_q', state.evidenceQuery);
  if (state.paperEvidenceQuery) params.set('within_q', state.paperEvidenceQuery);
  if (state.searchMode !== 'paper') params.set('search_mode', state.searchMode);
  if (state.yearFrom) params.set('from', String(state.yearFrom));
  if (state.yearTo) params.set('to', String(state.yearTo));
  if (state.openAccessOnly) params.set('oa', '1');
  if (state.sources.join(',') !== DEFAULT_SOURCES.join(',')) {
    params.set('sources', state.sources.join(','));
  }
  if (state.availability !== 'all') params.set('availability', state.availability);
  if (state.sort !== 'relevance') params.set('sort', state.sort);
  if (state.cursor && state.page > 1) {
    params.set('cursor', state.cursor);
    params.set('page', String(state.page));
    if (state.cursorTrail.length > 0) {
      params.set('trail', JSON.stringify(state.cursorTrail.slice(-20)));
    }
  }
  if (state.paperKey) params.set('paper', state.paperKey);
  if (state.evidenceId) params.set('evidence', state.evidenceId);
  if (state.pdfPage) params.set('pdf_page', String(state.pdfPage));
  if (state.pdfZoom && state.pdfZoom !== 1.1) {
    params.set('zoom', String(state.pdfZoom));
  }
  if (state.libraryMode !== 'search') {
    params.set('library', state.libraryMode);
  }
  if (state.libraryMode === 'collection' && state.collectionId) {
    params.set('collection', String(state.collectionId));
  }
  if (state.targetSessionId) params.set('session', state.targetSessionId);
  if (state.view !== 'pdf') params.set('view', state.view);
  return params;
}
