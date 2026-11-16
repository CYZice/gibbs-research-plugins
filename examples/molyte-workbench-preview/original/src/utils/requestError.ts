type ErrorData = Record<string, any>;

export interface RequestErrorSummary {
  message: string;
  code?: string;
  status?: number;
  isInsufficientBalance: boolean;
  required?: number;
  available?: number;
}

const INSUFFICIENT_CODES = new Set([
  'insufficient_balance',
  'insufficient_quota',
  'insufficient_funds',
  'quota_exceeded',
  'account_in_debt',
  'payment_required',
]);

const INSUFFICIENT_TEXT_PATTERN = /余额不足|额度不足|欠费|请充值|insufficient|quota|debt|payment required/i;

const firstString = (...values: any[]): string | undefined => {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return undefined;
};

const toFiniteNumber = (value: any): number | undefined => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
};

const normalizeValidationArray = (details: any[]): string => {
  const parts = details
    .map((item) => {
      if (!item || typeof item !== 'object') return '';
      const loc = Array.isArray(item.loc) ? item.loc.join('.') : '';
      const msg = typeof item.msg === 'string' ? item.msg : '';
      if (loc && msg) return `${loc}: ${msg}`;
      return msg || loc;
    })
    .filter(Boolean);
  return parts.join('; ');
};

export function summarizeRequestError(error: any, fallback = '操作失败'): RequestErrorSummary {
  const status = toFiniteNumber(error?.response?.status);
  const data: ErrorData = (error?.response?.data && typeof error.response.data === 'object')
    ? error.response.data
    : {};
  const detail = data?.detail;

  const codeRaw = firstString(
    data?.code,
    data?.error,
    typeof detail === 'object' && !Array.isArray(detail) ? detail?.code : undefined,
    typeof detail === 'object' && !Array.isArray(detail) ? detail?.error : undefined,
  );
  const code = codeRaw ? codeRaw.toLowerCase() : undefined;

  let message = '';
  if (typeof detail === 'string') {
    message = detail.trim();
  } else if (Array.isArray(detail)) {
    message = normalizeValidationArray(detail);
  } else if (detail && typeof detail === 'object') {
    message = firstString(
      detail.message,
      detail.detail,
      detail.reason,
      data?.message,
      data?.msg,
    ) || '';
  } else {
    message = firstString(data?.message, data?.msg, error?.message) || '';
  }
  if (!message) message = fallback;

  const detailsObj = (detail && typeof detail === 'object' && !Array.isArray(detail) && detail.details)
    ? detail.details
    : (data?.details && typeof data.details === 'object' ? data.details : {});

  const required = toFiniteNumber(
    detailsObj?.required
      ?? detailsObj?.required_quota
      ?? (detail && typeof detail === 'object' && !Array.isArray(detail) ? detail.required : undefined)
      ?? data?.required
  );
  const available = toFiniteNumber(
    detailsObj?.available
      ?? detailsObj?.available_quota
      ?? (detail && typeof detail === 'object' && !Array.isArray(detail) ? detail.available : undefined)
      ?? data?.available
  );

  const isInsufficientByCode = !!code && INSUFFICIENT_CODES.has(code);
  const isInsufficientByStatus = status === 402;
  const isInsufficientByMessage = INSUFFICIENT_TEXT_PATTERN.test(message);

  return {
    message,
    code,
    status,
    isInsufficientBalance: isInsufficientByCode || isInsufficientByStatus || isInsufficientByMessage,
    required,
    available,
  };
}

export function extractRequestErrorMessage(error: any, fallback = '操作失败'): string {
  return summarizeRequestError(error, fallback).message;
}

export function extractInsufficientBalanceMessage(error: any): string | null {
  const summary = summarizeRequestError(error, '可用余额不足，请充值后重试');
  return summary.isInsufficientBalance ? summary.message : null;
}
