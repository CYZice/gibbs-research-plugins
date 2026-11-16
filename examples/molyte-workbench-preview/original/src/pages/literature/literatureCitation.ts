import type {
  EvidenceSource,
  PaperLocation,
} from '../../api/literatureWorkspace';


export interface LiteratureCitationPaper {
  paper_id?: string;
  identity_key?: string;
  title: string;
  doi?: string;
  year?: number;
  venue?: string;
}

interface CitationLocation {
  page?: number;
  section_title?: string;
  bbox?: [number, number, number, number];
  coordinate_space?: 'normalized';
}

export interface CanonicalEvidenceCitation {
  schema_version: 'literature-citation/0.1';
  kind: 'canonical_evidence';
  paper: LiteratureCitationPaper;
  evidence_id: string;
  text: string;
  location?: CitationLocation;
  verification: 'canonical_evidence';
}

export interface UserSelectionCitation {
  schema_version: 'literature-citation/0.1';
  kind: 'user_selection';
  paper: LiteratureCitationPaper;
  text: string;
  location?: {
    page?: number;
    section_title?: string;
  };
  verification: 'user_selection';
}

export type LiteratureCitation = CanonicalEvidenceCitation | UserSelectionCitation;

export function normalizeSelectionText(value: string): string {
  return value.replace(/\s+/g, ' ').trim().slice(0, 4000);
}

function normalizedLocation(location?: PaperLocation): CitationLocation | undefined {
  if (!location) return undefined;
  const result: CitationLocation = {};
  if (Number.isInteger(location.page) && Number(location.page) > 0) {
    result.page = location.page;
  }
  if (location.section_title) result.section_title = location.section_title;
  if (
    location.coordinate_space === 'normalized'
    && Array.isArray(location.bbox)
    && location.bbox.length === 4
    && location.bbox.every((value) => (
      typeof value === 'number' && value >= 0 && value <= 1
    ))
    && location.bbox[2] > location.bbox[0]
    && location.bbox[3] > location.bbox[1]
  ) {
    result.bbox = location.bbox as [number, number, number, number];
    result.coordinate_space = 'normalized';
  }
  return Object.keys(result).length > 0 ? result : undefined;
}

export function buildCanonicalEvidenceCitation(
  workspacePaper: LiteratureCitationPaper,
  source: EvidenceSource,
): CanonicalEvidenceCitation {
  const sourcePaper = source.citation.paper;
  return {
    schema_version: 'literature-citation/0.1',
    kind: 'canonical_evidence',
    paper: {
      ...workspacePaper,
      paper_id: sourcePaper.paper_id,
      title: sourcePaper.title || workspacePaper.title,
      doi: sourcePaper.doi || workspacePaper.doi,
      year: sourcePaper.year || workspacePaper.year,
      venue: sourcePaper.venue || workspacePaper.venue,
    },
    evidence_id: source.citation.evidence_id,
    text: source.citation.quote,
    location: normalizedLocation(source.citation.location),
    verification: 'canonical_evidence',
  };
}

export function buildUserSelectionCitation(
  paper: LiteratureCitationPaper,
  selectedText: string,
  page?: number,
): UserSelectionCitation {
  const text = normalizeSelectionText(selectedText);
  if (!text) throw new Error('Selection text is empty');
  return {
    schema_version: 'literature-citation/0.1',
    kind: 'user_selection',
    paper,
    text,
    location: page ? { page } : undefined,
    verification: 'user_selection',
  };
}

export function formatLiteratureSource(
  citation: LiteratureCitation,
): string {
  const paper = citation.paper;
  const identity = paper.doi || paper.venue || paper.paper_id || '来源待补';
  const page = citation.location?.page ? `第 ${citation.location.page} 页` : '页码待解析';
  const section = citation.location?.section_title
    ? `，${citation.location.section_title}`
    : '';
  const evidence = citation.kind === 'canonical_evidence'
    ? `。Evidence: ${citation.evidence_id}`
    : '。用户选中文段';
  return `${paper.title}${paper.year ? `（${paper.year}）` : ''}. ${identity}. ${page}${section}${evidence}`;
}
