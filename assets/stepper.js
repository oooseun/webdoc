// stepper.js — webdoc's one interactive primitive.
// Turns any [data-stepper] region into a click-to-advance state machine:
// show one .step at a time, move with Prev/Next/Reset or arrow keys. No autoplay,
// no looping — the reader controls the pace. Inert if no [data-stepper] is present.
(function () {
  "use strict";

  function initStepper(root) {
    var steps = Array.prototype.slice.call(root.querySelectorAll(".step"));
    if (steps.length === 0) return;

    var prev = root.querySelector("[data-stepper-prev]");
    var next = root.querySelector("[data-stepper-next]");
    var reset = root.querySelector("[data-stepper-reset]");
    var status = root.querySelector("[data-stepper-status]");
    var index = 0;

    function render() {
      steps.forEach(function (step, i) {
        var active = i === index;
        step.hidden = !active;
        step.setAttribute("aria-hidden", active ? "false" : "true");
      });
      if (status) status.textContent = (index + 1) + " / " + steps.length;
      if (prev) prev.disabled = index === 0;
      if (next) next.disabled = index === steps.length - 1;
    }

    function go(delta) {
      var target = index + delta;
      if (target < 0 || target >= steps.length) return;
      index = target;
      render();
    }

    if (prev) prev.addEventListener("click", function () { go(-1); });
    if (next) next.addEventListener("click", function () { go(1); });
    if (reset) reset.addEventListener("click", function () { index = 0; render(); });

    root.setAttribute("tabindex", "0");
    root.addEventListener("keydown", function (event) {
      if (event.key === "ArrowRight" || event.key === "ArrowDown") { go(1); event.preventDefault(); }
      else if (event.key === "ArrowLeft" || event.key === "ArrowUp") { go(-1); event.preventDefault(); }
      else if (event.key === "Home") { index = 0; render(); event.preventDefault(); }
      else if (event.key === "End") { index = steps.length - 1; render(); event.preventDefault(); }
    });

    render();
  }

  function initAll() {
    Array.prototype.slice
      .call(document.querySelectorAll("[data-stepper]"))
      .forEach(initStepper);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initAll);
  } else {
    initAll();
  }
})();
