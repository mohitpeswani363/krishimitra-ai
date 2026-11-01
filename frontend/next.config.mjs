/** @type {import('next').NextConfig} */
// Static export: the FastAPI server serves frontend/out on the same origin as /api.
const nextConfig = { output: 'export', trailingSlash: true };
export default nextConfig;
