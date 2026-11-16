import {
  CheckCircleFilled,
  LoadingOutlined,
} from '@ant-design/icons';
import type { LiteratureUpload } from '../../api/literatureWorkspace';

interface LiteratureUploadQueueProps {
  uploads: LiteratureUpload[];
  onRetry: (uploadId: number) => void;
}

export default function LiteratureUploadQueue({
  uploads,
  onRetry,
}: LiteratureUploadQueueProps) {
  if (uploads.length === 0) return null;

  return (
    <div className="literature-upload-queue" aria-label="PDF 处理任务">
      <div className="upload-queue-heading">
        <span>最近上传</span>
        <small>上传后自动解析并收藏</small>
      </div>
      {uploads.slice(0, 3).map((upload) => (
        <div className={`upload-task ${upload.status}`} key={upload.id}>
          <div>
            <strong title={upload.original_filename}>
              {upload.original_filename}
            </strong>
            <small>
              {upload.status === 'succeeded'
                ? '已入库'
                : upload.status === 'failed'
                  ? (upload.error_message || '处理失败')
                  : `${upload.stage || '等待处理'} · ${Math.round(upload.progress * 100)}%`}
            </small>
          </div>
          {upload.status === 'failed' ? (
            <button type="button" onClick={() => onRetry(upload.id)}>
              重试
            </button>
          ) : upload.status === 'succeeded' ? (
            <CheckCircleFilled />
          ) : (
            <LoadingOutlined />
          )}
        </div>
      ))}
    </div>
  );
}
