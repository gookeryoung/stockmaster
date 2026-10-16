// Tailwind v4 的 PostCSS 插件从 tailwindcss 包里拆了出来，必须用 @tailwindcss/postcss
// （v4 下再写 `tailwindcss: {}` 会直接报 "trying to use tailwindcss directly as a
// PostCSS plugin"）。v4 内置 lightningcss 处理厂商前缀，不再需要 autoprefixer。
export default {
  plugins: {
    "@tailwindcss/postcss": {},
  },
};
