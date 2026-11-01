// Small progressive enhancements. Everything works without JavaScript.
(function () {
  // Progress bar widths (CSP forbids inline style attributes).
  document.querySelectorAll("[data-flex]").forEach(function (el) {
    el.style.flex = el.getAttribute("data-flex");
  });

  // Time to Edit: seconds spent on a review form before submitting.
  document.querySelectorAll("form[data-timer]").forEach(function (form) {
    var start = Date.now();
    form.addEventListener("submit", function () {
      var field = form.querySelector('input[name="' + form.getAttribute("data-timer") + '"]');
      if (field) field.value = String(Math.round((Date.now() - start) / 1000));
    });
  });

  // Add glossary / entity rows.
  document.querySelectorAll(".add-row").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var tbody = document.querySelector("#" + btn.getAttribute("data-table") + " tbody");
      var prefix = btn.getAttribute("data-prefix");
      var fields = btn.getAttribute("data-fields").split(",");
      var i = tbody.querySelectorAll("tr").length;
      var tr = document.createElement("tr");
      fields.forEach(function (f) {
        var td = document.createElement("td");
        var input = document.createElement("input");
        input.name = prefix + "_" + f + "_" + i;
        td.appendChild(input);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
  });

  document.querySelectorAll(".back-link").forEach(function (a) {
    a.addEventListener("click", function (e) {
      if (history.length > 1) { e.preventDefault(); history.back(); }
    });
  });
})();
