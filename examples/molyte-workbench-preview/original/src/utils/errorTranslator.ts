/**
 * MD 计算错误信息翻译器
 * 将技术性错误信息翻译成用户友好的中文提示
 */
import { summarizeRequestError } from './requestError';

// 平台内部异常（后端 bug / 未捕获异常）的特征。命中这些就说明是平台侧问题，
// 不能把 Python 原文（cannot access local variable / Traceback / Server Error: ...）
// 当成"用户表单填错"弹给用户。
export const PLATFORM_BUG_PATTERN =
  /server error|internal server error|cannot access local variable|traceback|unhandled exception|is not defined|object has no attribute|referenced before assignment|where it is not associated with a value/i;

export const PLATFORM_BUG_MESSAGE =
  '服务器在处理这次请求时出现内部异常，这是平台侧的问题，不是你的表单填错了。平台已记录这次错误，可以稍后再试一次；若反复出现，请把所在模块和操作时间反馈给我们。';

/**
 * 把"提交 / 创建任务失败"的后端错误转成对用户友好的一句话。
 * 优先识别平台内部异常，避免把后端 Python 异常原文直接弹给用户；
 * 其余情况沿用后端返回的（已经友好的）消息。
 */
export function describeSubmitError(error: any, fallback = '提交失败，请稍后重试'): string {
  const summary = summarizeRequestError(error, fallback);
  const msg = summary.message || '';
  if (PLATFORM_BUG_PATTERN.test(msg)) return PLATFORM_BUG_MESSAGE;
  // 5xx 但后端没给出可读消息时，也兜底成平台异常话术。
  if ((summary.status ?? 0) >= 500 && (!msg || msg === fallback)) return PLATFORM_BUG_MESSAGE;
  return msg || fallback;
}

export interface TranslatedError {
  title: string;
  description: string;
  suggestion: string;
  severity: 'error' | 'warning';
  originalError?: string;
}

const errorPatterns: Array<{
  pattern: RegExp;
  translate: (match: RegExpMatchArray, original: string) => TranslatedError;
}> = [
  {
    pattern: /No template is found for BondTemplate|cannot find a template|you have a bond for which I cannot find a template/i,
    translate: (_, original) => ({
      title: '交联成键模板缺失',
      description: '当前单体/交联剂的反应位点组合没有匹配到可用的成键模板',
      suggestion: '请优先使用已验证的交联规则，或调整单体与交联剂角色、反应位点和交联度后重新提交',
      severity: 'error',
      originalError: original,
    }),
  },
  {
    pattern: /交联模板闭包预检失败|generic radical closure precheck failed|generic radical preflight failed/i,
    translate: (_, original) => ({
      title: '交联模板预检失败',
      description: '系统在正式成网前检查到闭包反应或拓扑模板不可用',
      suggestion: '请检查主单体、交联剂 SMILES、反应规则和角色是否匹配，再降低交联度做一次小体系验证',
      severity: 'error',
      originalError: original,
    }),
  },
  {
    pattern: /grompp.*returncode\s+(97|1)|returncode\s+(97|1).*grompp|Fatal error:.*grompp/i,
    translate: (_, original) => ({
      title: 'GROMACS 拓扑预检失败',
      description: 'grompp 阶段没有通过，通常是拓扑、原子类型、键角参数或初始结构不一致',
      suggestion: '请查看技术详情中的 grompp 输出；优先确认组件参数是否完整、体系规模是否过大、初始结构是否合理',
      severity: 'error',
      originalError: original,
    }),
  },
  {
    pattern: /running_jobs.*(set object does not support item assignment|no attribute)|DummyWorker.*running_jobs/i,
    translate: (_, original) => ({
      title: 'Worker 状态记录异常',
      description: '后台 worker 的运行任务状态容器不符合交联任务处理器预期',
      suggestion: '这是服务端实现问题，需要修正 worker/canary 适配后再重试，不建议反复提交同一配方',
      severity: 'error',
      originalError: original,
    }),
  },
  {
    pattern: /RESP input '([^']+)'.*radical centers|RESP failed for ([^:]+): radical SMILES detected|RESP.*radical|radical.*SMILES|自由基.*SMILES/i,
    translate: (match, original) => {
      const molecule = (match[1] || match[2] || '').trim();
      const moleculeHint = molecule ? `（问题分子：${molecule}）` : '';
      return ({
      title: 'RESP 输入不合法',
      description: `检测到自由基或开壳层 SMILES，当前流程要求闭壳层分子${moleculeHint}`,
      suggestion: molecule ? `请检查 ${molecule} 的 SMILES，改为闭壳层形式后重新提交` : '请把分子改为闭壳层 SMILES 后重新提交',
      severity: 'error',
      originalError: original,
    })},
  },
  {
    pattern: /RESP calculation failed|RESP failed/i,
    translate: (_, original) => ({
      title: 'RESP 电荷计算失败',
      description: 'RESP 阶段失败，无法继续构建力场参数',
      suggestion: '请查看技术详情；若含自由基请先修正 SMILES',
      severity: 'error',
      originalError: original,
    }),
  },
  {
    pattern: /Failed to run Packmol|packmol.*fail|Packmol error/i,
    translate: (_, original) => ({
      title: '分子装配失败',
      description: 'Packmol 阶段失败（可能由盒子尺寸、分子初始构型或输入文件导致）',
      suggestion: '请查看技术详情中的 packmol 输出并根据日志调整参数',
      severity: 'error',
      originalError: original,
    }),
  },
  {
    pattern: /Pair coeff for hybrid has invalid style|pair_hybrid/i,
    translate: (_, original) => ({
      title: '力场参数错误',
      description: '力场参数缺失',
      suggestion: '请检查分子是否有对应的力场参数',
      severity: 'error',
      originalError: original,
    }),
  },
  {
    pattern: /LAMMPS.*ERROR[:\s]+(.+)/i,
    translate: (_, original) => ({
      title: 'MD 模拟错误',
      description: '模拟运行出错',
      suggestion: '请检查参数设置或联系管理员',
      severity: 'error',
      originalError: original,
    }),
  },
  {
    pattern: /TIMEOUT|time.*limit|超时/i,
    translate: (_, original) => ({
      title: '计算超时',
      description: '计算时间不足',
      suggestion: '请减少模拟步数或增加计算时间',
      severity: 'error',
      originalError: original,
    }),
  },
  {
    pattern: /OUT.*OF.*MEMORY|oom|内存不足|memory/i,
    translate: (_, original) => ({
      title: '内存不足',
      description: '内存超限',
      suggestion: '请减少分子数量或盒子尺寸',
      severity: 'error',
      originalError: original,
    }),
  },
  {
    pattern: /CANCELLED|用户取消/i,
    translate: (_, original) => ({
      title: '任务已取消',
      description: '任务被取消',
      suggestion: '如需重新计算请复制配置后重新提交',
      severity: 'warning',
      originalError: original,
    }),
  },
  {
    pattern: /Slurm状态:\s*FAILED.*退出码:\s*(\d+):(\d+)/i,
    translate: (_, original) => ({
      title: '计算失败',
      description: '任务运行出错',
      suggestion: '请检查配置或查看日志',
      severity: 'error',
      originalError: original,
    }),
  },
];

export function translateError(errorMessage: string | null | undefined): TranslatedError | null {
  if (!errorMessage) return null;

  for (const { pattern, translate } of errorPatterns) {
    const match = errorMessage.match(pattern);
    if (match) return translate(match, errorMessage);
  }

  return {
    title: '计算失败',
    description: '任务运行出错',
    suggestion: '请查看日志或联系管理员',
    severity: 'error',
    originalError: errorMessage,
  };
}
