# Day 3 — Weer- en uitjes-MCP-server + Agent Bricks-agent

Een MCP-server die het Nederlandse weer en een corpus van dagje-uit-locaties
ontsluit, en een Agent Bricks-agent die daarmee vragen beantwoordt als *"kunnen we
morgen naar buiten in Drunen, en zo niet, wat kunnen we dan doen?"*

Gebouwd op de weer- en uitjesapp van [Day 2](https://github.com/hwspuijbroek/homework-app-day-2/tree/weer-en-uitjes):
dezelfde bronnen, dezelfde drempelwaarden, hetzelfde Lakebase-corpus — maar waar
Day 2 een Flask-app was die zelf een taalmodel aanstuurde, is dit een set tools
waar een agent zelf mee redeneert.

## Architectuur

```
Agent Bricks-agent  --(MCP tool calls)-->  mcp_server/weather_mcp_server.py
                                                      |
                                    +-----------------+------------------+
                                    |                 |                  |
                             weather_client.py    geocode.py        venues.py
                                    |                 |                  |
                              Buienradar         Nominatim          Lakebase
                          (metingen + 7-daagse   (NL-plaatsnamen   (poi_documents +
                           verwachting per          -> lat/lon)     poi_embeddings,
                           coördinaat)                              geseed in Day 2)
                                    |
                               verdict.py  (drempelwaarden: binnen / buiten / gemengd)
```

Eén Databricks App (`mcp_server/`), geregistreerd als external MCP. De agent is de
enige client; er is bewust geen dashboard — de optionele extra-credit-app uit de
opdracht zou een tweede kopie van deze modules nodig hebben, en dat is pas de moeite
waard als er iets te tonen valt dat de agent zelf niet toont.

## De zes tools

| Tool | Wat het doet | Bron |
| --- | --- | --- |
| `get_current_weather(location)` | Gemeten condities bij het dichtstbijzijnde station | Buienradar |
| `get_forecast(location, days)` | Verwachting tot 7 dagen, vandaag eerst | Buienradar |
| `get_outdoor_advice(location, day)` | **Het oordeel**: binnen, buiten of gemengd, met de reden | verdict.py |
| `get_best_day(location)` | De dagen gerangschikt op geschiktheid om eropuit te gaan | verdict.py |
| `get_rain_timing(location, day)` | Per uur: wanneer het regent, en hoeveel | Buienradar |
| `find_activities(location, query, radius_km, day, limit)` | Uitjes in de buurt, gesplitst op binnen/buiten, gekoppeld aan het weeroordeel én aan de openingstijden van die dag | Lakebase + Buienradar |

De opdracht vraagt om minimaal drie tools waarvan één een afgeleid oordeel is. Dat
is `get_outdoor_advice`, en het is geen doorgeefluik:

- **hoeveelheid vóór kans.** 3 mm neerslag overdag is 'binnen' op zichzelf; 1 mm
  plus 50% kans ook. Een kans van 80% op 0,2 mm motregen is dat níét — dat is een
  prima middag, en scoren op de kans alleen liet in Day 2 de natste dag van de week
  als beste dag uit de bus komen.
- **een comfortband, geen maximum.** Onder 4 graden en boven 30 is het 'binnen';
  22 graden verslaat 31 in de rangschikking.
- **een onweersveto.** "Kans op enkele pittige (onweers)buien" draagt een láág
  dagcijfer voor regen. Zo'n omschrijving trekt een verder mooie dag terug naar
  'gemengd'.
- **drie uitkomsten, niet twee.** 'gemengd' is de eerlijke uitkomst als de
  verwachting geen sterkere uitspraak draagt, en de agent biedt dan allebei aan.

De drempelwaarden en de redenering erachter staan in
[verdict.py](mcp_server/verdict.py); ze zijn overgenomen uit Day 2, waar ze tegen
twee weken echte Buienradar-output zijn afgesteld.

### Openingstijden

Een uitje aanraden dat die dag dicht is, is een antwoord dat klopt tot je voor de
deur staat. Dat gebeurde ook: gevraagd naar zaterdag beval de agent het
Geniemuseum aan, met in datzelfde antwoord de openingstijden
`Tu-Th 10:00-16:00; Jul-Aug Su 10:00-16:00`.

[opening_hours.py](mcp_server/opening_hours.py) leest die OSM-notatie nu tegen de
gevraagde datum en zet per locatie `waarschijnlijk_open` op true, false of
**null**. Die derde waarde is het punt: OSM's `opening_hours` is een volledige
grammatica met feestdagen, weeknummers, zonsondergang en vrije tekst, en een
parser die de helft snapt is gevaarlijker dan geen parser — een onterecht
"geopend" is precies het zelfverzekerd-verkeerde antwoord waar dit project
tegen ontworpen is. Dus: een smalle deelverzameling, en alles daarbuiten wordt
geweigerd.

Gemeten op het echte corpus: 483 van de 1903 locaties hebben openingstijden,
waarvan 85% leesbaar is voor deze lezer, en op zaterdag 29 augustus staan er 31
op dicht. Feestdagen worden bewust niet meegerekend, en dat staat in de caveats
van het antwoord.

## Weer-API en authenticatie

**Buienradar** — geen account, geen API-sleutel, geen registratie. Twee endpoints:
de [gedocumenteerde open-data feed](https://data.buienradar.nl/2.0/feed/json) voor
metingen van ~40 stations, en `forecast.buienradar.nl` voor de verwachting per
coördinaat. Die tweede is Buienradar's eigen app-backend en draagt geen
gebruiksvoorwaarden; er wordt niets uit opgeslagen, dus als hij verdwijnt kost dat
precisie en niet de server. Plaatsnamen gaan via
[Nominatim](https://nominatim.openstreetmap.org/) (OpenStreetMap), eveneens zonder
sleutel, met een verplichte User-Agent.

**Dus: geen secret voor de weerkant.** Het enige secret in dit project is de
Lakebase-URL voor het uitjes-corpus, en dat volgt exact het patroon uit de
opdracht — `WorkspaceClient().secrets.get_secret()`, base64-gedecodeerd, scope en
key instelbaar via `app.yaml` ([lakebase.py](mcp_server/lakebase.py)). Er staat
geen sleutel in de repo en er is er ook geen nodig om de weertools te draaien.

**Reikwijdte: alleen Nederland.** Buienradar heeft geen data daarbuiten, dus een
vraag over Chicago komt terug als `reason: "location_unknown"` in plaats van als
een schatting. De voorbeeldvragen in de opdracht zijn Amerikaanse steden; deze
inzending beantwoordt de Nederlandse variant daarvan, net zoals de NWS-optie uit
de opdracht alleen de VS zou dekken.

## Lokaal draaien

De dev container ([.devcontainer/](.devcontainer/)) installeert alles: Python 3.11,
de Databricks CLI, CPU-only torch, en de Databricks AI Dev Kit. Open de map in
VS Code en kies **Reopen in Container**.

```bash
databricks auth login --host <workspace-url>   # nodig voor find_activities
cd mcp_server && python weather_mcp_server.py  # MCP op http://localhost:8000
```

De vijf weertools werken zonder Databricks-login; alleen `find_activities` heeft de
Lakebase-secret nodig.

Testen (208 tests, geen netwerk, geen database):

```bash
pytest tests -q
```

Wat er getest wordt: de drempelwaarden in `verdict.py`, het onderscheid tussen
"plaats onbekend" en "dienst plat" in `geocode.py`, het combineren in
`weather_service.py`, de SQL- en rangschikkingsregels in `venues.py`, het lezen
van de openingstijden, het ontleden van Buienradar's dagdelen in
`weather_client.py`, en de MCP-oppervlakte zelf — dat alle zes tools registreren
mét beschrijving, en dat geen enkele fout als exception ontsnapt.

```bash
pytest tests -q --cov=mcp_server --cov-report=term-missing
```

Dekking is 86%, en de verdeling is leerzamer dan het totaal: 94–98% op de
modules die voor day 3 geschreven zijn, 87% op `weather_client.py`, en 27% op
`lakebase.py` — dat laatste is vrijwel helemaal verbindingsherstel, dat je
zonder echte database niet zinnig test.

Die meting was geen formaliteit. `fetch_local_forecast` stond op 59% en bleek
ongetest, terwijl het de functie is die `daytime_precipitation_mm` produceert:
het cijfer waar de drempel van 3 mm op staat, waar `day_score` op rangschikt, en
dat het hele verschil tussen hoeveelheid en kans draagt. De tests die Day 2
meegaf, testten het *station*-endpoint — een ander endpoint. Dat gat is nu
dicht, inclusief de regel dat regen in de nacht niet tegen de dag telt.

### De server als MCP-client aanspreken

De testsuite stubt Buienradar, Nominatim en Lakebase — dat maakt hem snel en
voorspelbaar, en betekent ook dat hij nooit bewijst dát de server MCP spreekt.
Daarvoor is [tools/smoke.py](tools/smoke.py): dat verbindt als echte client over
streamable HTTP, dezelfde transportlaag die de Databricks MCP-gateway gebruikt,
en roept alle zes de tools aan tegen de echte bronnen.

```bash
cd mcp_server && python weather_mcp_server.py   # terminal 1
python tools/smoke.py                           # terminal 2
python tools/smoke.py https://<app-url>/mcp     # of tegen de gedeployde app
```

Bewust inclusief de faalgevallen — Chicago, een onbekend dagwoord, een datum
voorbij de horizon. Een smoke-test die alleen het gelukkige pad laat zien had de
twee fouten gemist die deze wél vond: een horizon van vijf dagen die elke maandag
"zaterdag" moest weigeren, en een kandidatenlijst die Bergen (Noord-Holland)
dubbel toonde plus een buurt in Eindhoven.

`find_activities` heeft de Lakebase-secret nodig; zonder Databricks-login meldt
die netjes `service_unavailable` — wat meteen het degradatiepad laat zien.

## Deployen

1. **Git folder** in Databricks aanmaken voor deze repo (zoals Day 2, stap 7).
2. **Compute > Apps > Create app > Custom**, bijvoorbeeld `weer-uitjes-mcp`, met als
   bron de submap `mcp_server/` — daar staan `app.yaml` en `requirements.txt`.
3. Zorg dat het secret `database/lakebase-url` bestaat (uit Day 2) en dat de
   service principal van de app het mag lezen. Andere scope? Pas
   `LAKEBASE_SECRET_SCOPE` / `LAKEBASE_SECRET_KEY` in
   [app.yaml](mcp_server/app.yaml) aan.
4. Deploy, en noteer de app-URL.

## De agent bouwen

1. **AI Gateway > MCPs > Add MCP**: plak de app-URL als streamable-HTTP endpoint,
   noem hem `nl-weather-and-days-out`. Databricks leest de zes tools uit.
2. **Agents > Agent Bricks > Create agent**, type **Custom LLM**.
3. Koppel de MCP-server als tool.
4. Plak de systeemprompt uit [agent/system_prompt.md](agent/system_prompt.md).

Die prompt hangt aan de `reason`-codes die de tools teruggeven
(`location_unknown`, `service_unavailable`, `date_out_of_range`,
`internal_error`), zodat een herformulering van een foutmelding de guardrails niet
stukmaakt.

## Demonstratie

> **In te vullen na het deployen** — de opdracht vraagt om minimaal drie
> natuurlijke-taalvragen met de tool-calls en de antwoorden van de agent.
> Voorgestelde drie, omdat ze elk iets anders aantonen:
>
> 1. *"Kan ik morgen naar buiten in Drunen?"* — één tool-call
>    (`get_outdoor_advice`), en een antwoord dat het beslissende cijfer noemt.
> 2. *"Het regent zaterdag, wat kunnen we dan doen met kinderen in de buurt van
>    Den Bosch?"* — `find_activities` met de vraag van de gebruiker als `query`;
>    laat zien dat `lead_with` de binnen-lijst vooropzet.
> 3. *"Wordt het mooi weer in Chicago dit weekend?"* — de weigering. De agent
>    hoort te zeggen dat hij alleen Nederlandse plaatsen kan opzoeken, en géén
>    schatting te geven.

## Verantwoording van hergebruik

Uit Day 2 overgenomen, ongewijzigd: `weather_client.py` (Buienradar-adapter),
`lakebase.py`, `migrations/`, en de drempelwaarden in `verdict.py`.

Aangepast, met reden:

- **`geocode.py`** — Day 2 gaf `None` terug voor zowel "plaats bestaat niet" als
  "Nominatim antwoordde niet". Een agent moet die twee uit elkaar houden: het
  eerste betekent *vraag de gebruiker om een andere plaats*, het tweede *zeg dat
  de dienst plat ligt en gok niet*. Nu is dat een `None` versus een exception.
  Verder onthoudt de opzoeker wat hij al opzocht — Nominatim is een
  vrijwilligersdienst die om hooguit één verzoek per seconde vraagt, en één
  gesprek zoekt dezelfde plaats vier keer op — en geeft hij bij een dubbelzinnige
  naam de kandidaten mee, zodat de agent kan vrágen welke Bergen bedoeld wordt in
  plaats van achteraf te melden welke het werd.
- **`venues.py`** — de LLM-rerank uit Day 2 is eruit. Daar bezat de app het
  taalmodel; hier ís de agent dat model, en die krijgt de kandidaten mét
  `similarity` en `distance_km` om zelf te wegen. Een tweede modelaanroep binnen
  een tool zou latency en een extra faalpad kosten voor hetzelfde oordeel. Wel
  moet de agent dat dan echt doen: tegen het echte corpus gemeten liggen de
  scores dicht op elkaar (0,78–0,83) en eindigde op "iets met dieren" een
  bowlingbaan bóven Dierenpark De Oliemeulen. Dat staat nu expliciet in de
  tool-beschrijving en in de systeemprompt.
- **`weather_service.py`** — nieuw. De Flask-routes van Day 2 combineerden
  geocoding, verwachting, oordeel en locaties; dat combineren zit nu hier, zodat
  de tools dun blijven. Inclusief Day 2's `resolve_target_date`: weekdagen en
  "weekend" worden hier omgerekend en niet door de agent, want datumrekenen is
  wat taalmodellen stil fout doen — een geldige verwachting voor de verkeerde dag
  ziet er nergens verkeerd uit.
- **`get_best_day` zegt het als de winnaar toch een binnendag is.** De twee
  oordelen in `verdict.py` — drempels voor één dag, strafpunten voor de
  rangschikking — hoeven het niet met elkaar eens te zijn. Waren ze dat niet, dan
  kreeg de agent "zaterdag is de beste dag" en "zaterdag kun je beter binnen
  blijven" in één antwoord. Nu draagt de rangschikking `best_is_outdoor_worthy`
  en een waarschuwing in `note`.
- **Geen Lakebase voor het weer.** Day 2 las de stations uit Postgres, waar
  `/weather/sync` ze had gezet. Hier komen ze rechtstreeks uit de feed: dezelfde
  data, één HTTP-call, en de weertools blijven werken als de database in
  auto-suspend staat.

## Bestanden

- [mcp_server/weather_mcp_server.py](mcp_server/weather_mcp_server.py) — de zes
  `@mcp.tool`-functies; dun, met de gebruiksaanwijzing voor de agent in de
  docstrings
- [mcp_server/weather_service.py](mcp_server/weather_service.py) — het combineren,
  plus de drie foutsoorten
- [mcp_server/weather_client.py](mcp_server/weather_client.py) — Buienradar-adapter
  (uit Day 2)
- [mcp_server/geocode.py](mcp_server/geocode.py) — Nominatim-adapter
- [mcp_server/verdict.py](mcp_server/verdict.py) — de drempelwaarden
- [mcp_server/venues.py](mcp_server/venues.py) — het uitjes-corpus uit Lakebase
- [mcp_server/lakebase.py](mcp_server/lakebase.py) — Postgres-verbinding + secret
- [mcp_server/app.yaml](mcp_server/app.yaml) · [mcp_server/requirements.txt](mcp_server/requirements.txt)
  — Databricks App-configuratie
- [agent/system_prompt.md](agent/system_prompt.md) — de systeemprompt, met
  verantwoording
- [tests/](tests/) — 208 tests, geen netwerk

## Een detail dat ik bijna miste

FastMCP knipt een Google-style docstring uit elkaar: de samenvatting en de proza
eronder worden de beschrijving die de agent ziet, elke `Args:`-regel wordt de
beschrijving van dat argument in het JSON-schema — en de `Returns:`-sectie wordt
weggegooid. Alle uitleg over hoe je een antwoord moet lézen ("deze twee
regencijfers betekenen niet hetzelfde", "gemengd is geen zwak buiten") stond daar
eerst, en zou de agent dus nooit bereikt hebben. Die staat nu boven `Args:`.
[tests/test_mcp_tools.py](tests/test_mcp_tools.py) bewaakt het, want anders faalt
er niets als iemand het terugzet.
