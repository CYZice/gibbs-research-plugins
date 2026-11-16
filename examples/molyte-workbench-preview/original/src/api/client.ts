/**
 * Axios HTTP 客户端配置
 */
import axios from 'axios';
import type { AxiosError, AxiosResponse } from 'axios';
import { message } from 'antd';
import { getAccessToken, setAccessToken, clearAccessToken } from './token';
import {
  extractInsufficientBalanceMessage,
  extractRequestErrorMessage,
  summarizeRequestError,
} from '../utils/requestError';
import { PLATFORM_BUG_PATTERN, PLATFORM_BUG_MESSAGE } from '../utils/errorTranslator';

const isLocalhost = () =>
  ['localhost', '127.0.0.1', '::1'].includes(window.location.hostname);

const DEV_TOKEN_ENABLED = import.meta.env.DEV && import.meta.env.VITE_ENABLE_DEV_TOKEN === 'true';
const QUOTA_TOAST_THROTTLE_MS = 3000;
let lastQuotaToastAt = 0;
let lastQuotaToastMessage = '';

const notifyQuotaError = (text: string) => {
  const now = Date.now();
  if (now - lastQuotaToastAt < QUOTA_TOAST_THROTTLE_MS && text === lastQuotaToastMessage) {
    return;
  }
  lastQuotaToastAt = now;
  lastQuotaToastMessage = text;
  message.error(text);
};

const ensureDevToken = async (): Promise<string | null> => {
  if (!DEV_TOKEN_ENABLED || !isLocalhost()) return null;
  const existing = getAccessToken();
  if (existing) return existing;
  try {
    const res = await fetch('/api/v1/auth/dev-token', {
      method: 'POST',
      headers: { 'X-Dev-Auth': '1' },
    });
    if (!res.ok) return null;
    const data = await res.json();
    if (data?.access_token) {
      setAccessToken(data.access_token);
      return data.access_token;
    }
  } catch {
    // ignore
  }
  return null;
};

// 创建 axios 实例
const client = axios.create({
  baseURL: '/api/v1',
  timeout: 120000, // 增加到 120 秒，适应复杂计算
  headers: {
    'Content-Type': 'application/json',
  },
});

// 禁用响应缓存，确保每次都获取最新数据
client.defaults.headers.common['Cache-Control'] = 'no-cache, no-store, must-revalidate';
client.defaults.headers.common['Pragma'] = 'no-cache';
client.defaults.headers.common['Expires'] = '0';

// 请求拦截器 - 自动添加 token
client.interceptors.request.use(
  async (config) => {
    let token = getAccessToken();
    const url = typeof config.url === 'string' ? config.url : '';
    const isAuthEndpoint = url.startsWith('/auth/');
    if (!token && !isAuthEndpoint) {
      token = await ensureDevToken();
    }
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error) => {
    return Promise.reject(error);
  }
);

// 响应拦截器 - 统一错误处理
client.interceptors.response.use(
  (response: AxiosResponse) => {
    return response;
  },
  async (error: AxiosError) => {
    if (axios.isCancel(error)) {
      return Promise.reject(error);
    }
    if (error.response) {
      // 服务器返回错误状态码
      const status = error.response.status;
      const data = error.response.data as any;
      const config: any = error.config || {};
      const url = typeof config?.url === 'string' ? config.url : '';
      const isDevTokenRequest = url.includes('/auth/dev-token');
      const isAuthEndpoint = url.startsWith('/auth/');
      const normalized = summarizeRequestError(error, error.message || '请求失败');
      const normalizedMessage = normalized.message;

      if (data && typeof data === 'object') {
        if (normalized.code && typeof data.code !== 'string') {
          data.code = normalized.code;
        }
        if (typeof data.detail !== 'string') {
          data.detail = normalizedMessage;
        }
        if (typeof data.message !== 'string') {
          data.message = normalizedMessage;
        }
      }

      const quotaMessage = extractInsufficientBalanceMessage(error);
      if (quotaMessage) {
        notifyQuotaError(quotaMessage);
        window.dispatchEvent(new CustomEvent('billing:insufficient', {
          detail: {
            message: quotaMessage,
            status,
            path: typeof config?.url === 'string' ? config.url : '',
          },
        }));
      }

      // 处理COS相关错误
      if (data?.code === 'UnavailableForLegalReasons' || data?.message?.includes('arrears')) {
        console.error('COS账户欠费错误，请检查账户余额');
        // 不在这里抛出错误，让调用者处理
      } else if (status === 401) {
        // Dev-only retry with local token
        if (DEV_TOKEN_ENABLED && isLocalhost() && !config._retry && !isDevTokenRequest && !isAuthEndpoint) {
          config._retry = true;
          const token = await ensureDevToken();
          if (token) {
            config.headers = config.headers || {};
            config.headers.Authorization = `Bearer ${token}`;
            return client.request(config);
          }
        }
        // 未授权，清除 token 并通知应用层处理跳转
        clearAccessToken();
        if (!isAuthEndpoint) {
          window.dispatchEvent(new CustomEvent('auth:expired', {
            detail: { from: `${window.location.pathname}${window.location.search}` },
          }));
        }
      } else if (status === 403) {
        console.error('权限不足');
      } else if (status === 404) {
        console.error('资源不存在');
      } else if (status >= 500) {
        console.error('服务器错误');
        // 全局兜底：万一某个 5xx 把后端 Python 异常原文（cannot access local variable /
        // Server Error: ...）漏到了 detail，这里改写成友好话术，避免被各页面原样弹给用户。
        if (data && typeof data === 'object') {
          const rawDetail = typeof data.detail === 'string' ? data.detail : '';
          if (!rawDetail || PLATFORM_BUG_PATTERN.test(rawDetail)) {
            data.detail = PLATFORM_BUG_MESSAGE;
            data.message = PLATFORM_BUG_MESSAGE;
          }
        }
      }
    } else if (error.request) {
      // 请求已发送但没有收到响应
      console.error('网络错误，请检查网络连接');
    } else {
      // 其他错误
      console.error('请求失败:', error.message);
    }

    return Promise.reject(error);
  }
);

export default client;
