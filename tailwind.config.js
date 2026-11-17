/** Styles de COMPANIE FINDER : identité « signalisation routière ». */
module.exports = {
  content: ["./templates/**/*.html", "./static/*.js"],
  theme: {
    extend: {
      colors: {
        paper: "#eff2f1",
        card: "#ffffff",
        line: "#d5dcda",
        ink: { 400: "#8d979f", 500: "#64707a", 700: "#3a444b", 800: "#2b343a", 900: "#1c2328", 950: "#12171b" },
        // « pine » reste le nom de la couleur principale dans les gabarits : bleu panneau.
        pine: { 50: "#e8eff8", 100: "#d3e1f2", 600: "#2360a8", 700: "#1b4f8c", 800: "#143c6b" },
        clay: { 50: "#fff6dd", 700: "#8a5a00" },
        signal: "#f5c400",
      },
      fontFamily: {
        sans: ["Overpass", "Segoe UI", "Arial", "sans-serif"],
        display: ["Overpass", "Segoe UI", "Arial", "sans-serif"],
      },
      boxShadow: {
        card: "0 1px 0 rgba(18, 23, 27, 0.04)",
      },
    },
  },
};
