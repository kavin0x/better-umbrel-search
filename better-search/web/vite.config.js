import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/graphql": "http://127.0.0.1:8787",
      "/content": "http://127.0.0.1:8787",
    },
  },
});
