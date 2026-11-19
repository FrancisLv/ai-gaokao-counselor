/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_PRINCIPAL_ID?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
