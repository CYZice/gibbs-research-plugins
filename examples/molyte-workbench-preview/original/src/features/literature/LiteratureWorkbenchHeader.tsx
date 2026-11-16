import type { RefObject } from 'react';
import {
  BookOutlined,
  LoadingOutlined,
  SearchOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import type {
  LiteratureSearchMode,
} from '../../pages/literatureWorkspaceState';

interface LiteratureWorkbenchHeaderProps {
  searchMode: LiteratureSearchMode;
  query: string;
  searchScopeLabel: string;
  loading: boolean;
  uploading: boolean;
  serviceStatus: string;
  serviceClassName: string;
  uploadInputRef: RefObject<HTMLInputElement>;
  onQueryChange: (query: string) => void;
  onSearchModeChange: (mode: LiteratureSearchMode) => void;
  onSubmitSearch: () => void;
  onUploadPdf: (file: File) => void;
  onChooseUpload: () => void;
}

export default function LiteratureWorkbenchHeader({
  searchMode,
  query,
  searchScopeLabel,
  loading,
  uploading,
  serviceStatus,
  serviceClassName,
  uploadInputRef,
  onQueryChange,
  onSearchModeChange,
  onSubmitSearch,
  onUploadPdf,
  onChooseUpload,
}: LiteratureWorkbenchHeaderProps) {
  return (
    <header className="literature-topbar">
      <div className="literature-heading">
        <div className="literature-mark"><BookOutlined /></div>
        <div>
          <h1>文献工作台</h1>
          <p>检索本地论文、上传 PDF，并把可追溯证据带回智能研发</p>
        </div>
      </div>
      <form
        className="literature-search"
        onSubmit={(event) => {
          event.preventDefault();
          onSubmitSearch();
        }}
      >
        <div className="literature-search-modes" role="tablist" aria-label="检索对象">
          <button
            type="button"
            role="tab"
            aria-selected={searchMode === 'paper'}
            className={searchMode === 'paper' ? 'active' : ''}
            onClick={() => onSearchModeChange('paper')}
          >
            论文
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={searchMode === 'evidence'}
            className={searchMode === 'evidence' ? 'active' : ''}
            onClick={() => onSearchModeChange('evidence')}
          >
            原文证据
          </button>
        </div>
        <SearchOutlined />
        <input
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
          placeholder={searchMode === 'evidence'
            ? `在${searchScopeLabel}中检索方法、数值或结论`
            : `在${searchScopeLabel}中检索论文；留空可浏览全部`}
          aria-label={searchMode === 'evidence' ? '跨论文检索 Evidence' : '检索论文'}
        />
        <button
          type="submit"
          disabled={loading || query.trim().length === 1 || (
            searchMode === 'evidence' && query.trim().length < 2
          )}
        >
          {loading ? <LoadingOutlined /> : '检索'}
        </button>
        <span className="literature-search-scope">{searchScopeLabel}</span>
      </form>
      <input
        ref={uploadInputRef}
        className="literature-upload-input"
        type="file"
        accept="application/pdf,.pdf"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) onUploadPdf(file);
        }}
      />
      <button
        type="button"
        className="literature-upload-button"
        disabled={uploading}
        onClick={onChooseUpload}
      >
        {uploading ? <LoadingOutlined /> : <UploadOutlined />}
        上传 PDF
      </button>
      <div className={`literature-service ${serviceClassName}`}>
        <span />
        {serviceStatus}
      </div>
    </header>
  );
}
