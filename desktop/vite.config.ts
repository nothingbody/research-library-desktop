import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import {cpSync, mkdirSync} from 'node:fs';
export default defineConfig({plugins:[react(), {name:'pdf-local-assets', closeBundle(){
  mkdirSync('dist/pdf-assets', {recursive:true});
  for(const dir of ['cmaps','standard_fonts','wasm']) cpSync('node_modules/pdfjs-dist/' + dir, 'dist/pdf-assets/' + dir, {recursive:true});
}}],base:'./',build:{outDir:'dist',sourcemap:true},server:{host:'127.0.0.1'}});
