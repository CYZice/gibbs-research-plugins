import apiClient from './client';
import type { LiteratureHit } from './literatureWorkspace';


export interface LiteraturePaperSnapshot {
  identity_key: string;
  local_paper_id?: string | null;
  doi?: string | null;
  title: string;
  authors: Array<Record<string, unknown>>;
  year?: number | null;
  venue?: string | null;
  origins: LiteratureHit['origins'];
  availability_snapshot: LiteratureHit['availability'];
}

export interface LiteratureSavedPaper extends LiteraturePaperSnapshot {
  id: number;
  is_favorite: boolean;
  collection_ids: number[];
  snapshot_kind: 'metadata_only';
  last_seen_at: string;
  created_at: string;
  updated_at: string;
}

export interface LiteratureSavedPaperMutation {
  deleted: boolean;
  saved_paper: LiteratureSavedPaper;
}

export interface LiteratureCollection {
  id: number;
  project_id?: number | null;
  name: string;
  description?: string | null;
  archived_at?: string | null;
  item_count: number;
  items: LiteratureSavedPaper[];
  created_at: string;
  updated_at: string;
}

export interface LiteratureCollectionDraft {
  name: string;
  description?: string | null;
  project_id?: number | null;
}

export interface LiteratureCollectionPatch {
  name?: string;
  description?: string | null;
  archived?: boolean;
}

export interface LiteratureCollectionItemMutation {
  created: boolean;
  saved_paper: LiteratureSavedPaper;
}

export async function listLiteratureSavedPapers(
  favoritesOnly = true,
): Promise<LiteratureSavedPaper[]> {
  const response = await apiClient.get<LiteratureSavedPaper[]>(
    '/literature/saved-papers',
    { params: { favorites_only: favoritesOnly } },
  );
  return response.data;
}

export async function saveLiteraturePaper(
  paper: LiteraturePaperSnapshot,
  isFavorite = true,
): Promise<LiteratureSavedPaper> {
  const response = await apiClient.post<LiteratureSavedPaper>(
    '/literature/saved-papers',
    { ...paper, is_favorite: isFavorite },
  );
  return response.data;
}

export async function setLiteraturePaperFavorite(
  savedPaperId: number,
  isFavorite: boolean,
): Promise<LiteratureSavedPaperMutation> {
  const response = await apiClient.patch<LiteratureSavedPaperMutation>(
    `/literature/saved-papers/${savedPaperId}`,
    { is_favorite: isFavorite },
  );
  return response.data;
}

export async function listLiteratureCollections(
  includeArchived = false,
): Promise<LiteratureCollection[]> {
  const response = await apiClient.get<LiteratureCollection[]>(
    '/literature/collections',
    { params: { include_archived: includeArchived } },
  );
  return response.data;
}

export async function createLiteratureCollection(
  draft: LiteratureCollectionDraft,
): Promise<LiteratureCollection> {
  const response = await apiClient.post<LiteratureCollection>(
    '/literature/collections',
    draft,
  );
  return response.data;
}

export async function patchLiteratureCollection(
  collectionId: number,
  patch: LiteratureCollectionPatch,
): Promise<LiteratureCollection> {
  const response = await apiClient.patch<LiteratureCollection>(
    `/literature/collections/${collectionId}`,
    patch,
  );
  return response.data;
}

export async function addLiteratureCollectionItem(
  collectionId: number,
  paper: LiteraturePaperSnapshot,
): Promise<LiteratureCollectionItemMutation> {
  const response = await apiClient.post<LiteratureCollectionItemMutation>(
    `/literature/collections/${collectionId}/items`,
    { paper },
  );
  return response.data;
}

export async function removeLiteratureCollectionItem(
  collectionId: number,
  savedPaperId: number,
): Promise<void> {
  await apiClient.delete(
    `/literature/collections/${collectionId}/items/${savedPaperId}`,
  );
}
