/**
 * 项目管理 API
 */
import client from './client';
import type { Project, ProjectCreate, ProjectUpdate, TutorialExample, TutorialResourceVisibility } from '../types';

export interface ProjectListParams {
  skip?: number;
  limit?: number;
}

export interface ProjectListResponse {
  items: Project[];
  total: number;
}

const normalizeProjectList = (payload: any): Project[] => {
  if (Array.isArray(payload)) return payload;
  if (Array.isArray(payload?.items)) return payload.items;
  return [];
};

/**
 * 获取所有项目
 */
export const getProjects = async (): Promise<Project[]> => {
  const limit = 100;
  let skip = 0;
  let all: Project[] = [];

  while (true) {
    const response = await client.get('/projects/', { params: { skip, limit } });
    const batch = normalizeProjectList(response.data);
    all = all.concat(batch || []);
    if (!batch || batch.length < limit) break;
    skip += limit;
  }

  return all;
};

/**
 * 分页获取项目
 */
export const getProjectsPage = async (params: ProjectListParams = {}): Promise<ProjectListResponse> => {
  const response = await client.get('/projects/', { params });
  const items = normalizeProjectList(response.data);
  return {
    items,
    total: Number(response.headers['x-total-count'] || response.data?.total || items.length || 0),
  };
};

/**
 * 获取单个项目
 */
export const getProject = async (id: number): Promise<Project> => {
  const response = await client.get(`/projects/${id}`);
  return response.data;
};

export const getProjectTutorialExamples = async (id: number): Promise<TutorialExample[]> => {
  const response = await client.get(`/projects/${id}/tutorial-examples`);
  return response.data;
};

export const getTutorialResourceVisibility = async (
  resourceType: string,
  resourceId: number,
): Promise<TutorialResourceVisibility> => {
  const response = await client.get('/projects/tutorial-resource', {
    params: {
      resource_type: resourceType,
      resource_id: resourceId,
    },
  });
  return response.data;
};

/**
 * 创建项目
 */
export const createProject = async (data: ProjectCreate): Promise<Project> => {
  const response = await client.post('/projects/', data);
  return response.data;
};

/**
 * 更新项目
 */
export const updateProject = async (id: number, data: ProjectUpdate): Promise<Project> => {
  const response = await client.put(`/projects/${id}`, data);
  return response.data;
};

/**
 * 删除项目
 */
export const deleteProject = async (id: number): Promise<void> => {
  await client.delete(`/projects/${id}`);
};
