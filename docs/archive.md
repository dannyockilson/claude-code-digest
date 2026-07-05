---
layout: default
title: Archive
---

# Digest Archive

All published digests, newest first.

{% assign digests = site.pages | where_exp: "p", "p.dir == '/digests/'" | sort: "date" | reverse %}
{% for d in digests %}- [{{ d.date | date: "%d %b %Y" }}]({{ d.url | relative_url }}){% if d.special_edition %} — Special Edition{% endif %}
{% endfor %}
