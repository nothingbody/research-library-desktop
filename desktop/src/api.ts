export type Data = Record<string, any>;
declare global {
  interface Window {
    research: {
      call: (method: string, params?: Data) => Promise<any>;
      files: (kind: string, params?: Data) => Promise<any>;
      dropped: (files: FileList | File[]) => Promise<any>;
      external: (url: string) => Promise<void>;
      clipboard: (text: string) => Promise<void>;
      assistant: {
        status: () => Promise<{hasKey: boolean; encrypted: boolean}>;
        saveKey: (value: string) => Promise<{hasKey: boolean; encrypted: boolean}>;
        deleteKey: () => Promise<{hasKey: boolean; encrypted: boolean}>;
      };
      window: (action: string) => void;
      on: (callback: (event: string, data: any) => void) => () => void;
    };
  }
}
export const api = (method: string, params: Data = {}): Promise<any> => window.research.call(method, params);
export const files = (kind: string, params: Data = {}): Promise<any> => window.research.files(kind, params);
export const authorText = (item: Data) => item.authorText || (item.author || item.authors || []).map((a: Data) => a.literal || [a.given, a.family].filter(Boolean).join(' ')).join('; ');
export const yearText = (item: Data) => String(item.year || item.issued?.['date-parts']?.[0]?.[0] || '');
export const sizeText = (bytes: number) => bytes > 1024 ** 3 ? (bytes / 1024 ** 3).toFixed(1) + ' GB' : bytes > 1024 ** 2 ? (bytes / 1024 ** 2).toFixed(1) + ' MB' : Math.round(bytes / 1024) + ' KB';
export const stateText: Data = {unread: '未读', reading: '阅读中', read: '已读'};
export const dateText = (value: any) => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString('zh-CN') : '暂无';
export function useErrorText(error: any) {return String(error?.message || error).replace(/^Error invoking remote method '[^']+': Error: /, '');}
