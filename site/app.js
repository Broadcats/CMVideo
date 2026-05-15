/* OS detection - swap the primary download button + highlight the
 * matching tile in the downloads grid. Pure progressive enhancement;
 * if JS is off the user still sees every download option below.
 */
(function () {
  "use strict";

  function detectOS() {
    var p = (navigator.userAgentData && navigator.userAgentData.platform) || "";
    var ua = navigator.userAgent || "";
    var combined = (p + " " + ua).toLowerCase();
    if (combined.indexOf("win") !== -1) return "windows";
    if (combined.indexOf("mac") !== -1) return "mac";
    if (combined.indexOf("linux") !== -1 || combined.indexOf("x11") !== -1) {
      return "linux";
    }
    return null;
  }

  var os = detectOS();
  var labels = {
    linux: { name: "Linux", meta: ".tar.gz \u00b7 install.sh" },
    windows: { name: "Windows", meta: ".zip \u00b7 install.ps1" },
    mac: { name: "macOS", meta: ".tar.gz \u00b7 untested" }
  };

  var primary = document.getElementById("primary-download");
  var primaryMeta = document.getElementById("primary-download-meta");
  var match = os ? document.querySelector('.dl-card[data-os="' + os + '"]') : null;

  if (match && primary && primaryMeta) {
    primary.setAttribute("href", match.getAttribute("href"));
    primary.querySelector(".btn-label").textContent =
      "Download for " + labels[os].name;
    primaryMeta.textContent = labels[os].meta;
    match.classList.add("recommended");
  } else if (primary && primaryMeta) {
    primary.setAttribute("href", "#downloads");
    primaryMeta.textContent = "pick your OS below";
  }
})();
