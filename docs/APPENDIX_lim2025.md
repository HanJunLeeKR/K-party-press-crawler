# Appendix: what was supplemented from Lim (2025)

This repository and the Hugging Face dataset were collected from the parties' own websites. In one place the dataset also contains material from another published corpus:

> Lim, T. H. (2025). South Korean election campaign booklet and party statements corpora. *Scientific Data* 12, 1030. https://doi.org/10.1038/s41597-025-05220-4
> Data: Open Science Framework, https://doi.org/10.17605/OSF.IO/RCT9Y (file `sk_party_statements_v2022.parquet`).

## 1. What was added: 19 People Power Party documents

When we compared our People Power Party (PPP) file with the conservative-lineage series of Lim (2025), 115 of its 35,115 rows had no title match in our file. We then searched for the first and a middle 60-character snippet of each text in the full text of all 55,350 PPP documents we had collected. Results:

| Result | Rows |
|---|---|
| Present in our file under a slightly different title (for example HTML entities in titles, a tag such as `[보도자료]`, a prefix) | 94 |
| Too little text to test | 2 |
| Found nowhere in our collection | **19** |

The 19 documents were **appended to the end of `국민의힘_보도자료.csv`** (55,350 → 55,369 rows). The reason why the party website lacks them is not known (for example, deletion by the party is possible but not verified).

### How they are marked in the data

The schema has no extra column, so the origin is recorded in existing columns:

| Column | Value |
|---|---|
| `post_id` | `lim2025:conservative_<n>`, where `<n>` is the row identifier in Lim's file |
| `text_source` | `lim2025` |
| `source` | `lim2025_import` |
| `date_note` | a note (in Korean) that the date comes from Lim (2025) |
| `url`, `board_name`, `writer`, `fetched_at` | empty (not available in the source corpus) |

Title and text are copied from Lim's file (HTML entities in titles were decoded). To exclude these rows: `df[df["source"] != "lim2025_import"]`.

### The 19 documents

English glosses are ours, not official translations. Original Korean titles are in [`lim2025_imported_documents.csv`](lim2025_imported_documents.csv).

| Lim row | Date | `post_id` in dataset | Content |
|---|---|---|---|
| 597 | 2004-06-29 | `lim2025:conservative_597` | Statement: the party's "new start" must begin in Gwangju |
| 1510 | 2004-12-08 | `lim2025:conservative_1510` | Press release: closed-door briefing after an emergency caucus meeting |
| 13521 | 2012-04-01 | `lim2025:conservative_13521` | Park Geun-hye (Chair, Central Election Campaign Committee): visit to Gupo Market |
| 13522 | 2012-04-01 | `lim2025:conservative_13522` | Park Geun-hye: rally supporting candidate Son Su-jo |
| 13523 | 2012-04-01 | `lim2025:conservative_13523` | Park Geun-hye: rally in front of Goejeong Market |
| 13524 | 2012-04-01 | `lim2025:conservative_13524` | Park Geun-hye: visit to Danggam Market |
| 13526 | 2012-04-01 | `lim2025:conservative_13526` | Park Geun-hye: joint rallies in Busan districts and visit to Haebyeon Market |
| 13527 | 2012-04-01 | `lim2025:conservative_13527` | Park Geun-hye: vehicle campaign tour in Gimhae |
| 13530 | 2012-04-01 | `lim2025:conservative_13530` | Park Geun-hye: rally in Changwon, South Gyeongsang |
| 13531 | 2012-04-01 | `lim2025:conservative_13531` | Park Geun-hye: visit to Jayu Market and vehicle tour in Jinju |
| 13532 | 2012-04-01 | `lim2025:conservative_13532` | Park Geun-hye: vehicle campaign tour in Geoje |
| 13537 | 2012-04-02 | `lim2025:conservative_13537` | Park Geun-hye: visit to Chuncheon Pungmul Market |
| 13538 | 2012-04-02 | `lim2025:conservative_13538` | Park Geun-hye: vehicle campaign tour in Hongcheon |
| 13539 | 2012-04-02 | `lim2025:conservative_13539` | Park Geun-hye: visit to Sokcho Jungang Market |
| 13540 | 2012-04-02 | `lim2025:conservative_13540` | Park Geun-hye: visit to Gangneung Jungang Market |
| 13542 | 2012-04-02 | `lim2025:conservative_13542` | Park Geun-hye: vehicle campaign tour in Samcheok |
| 14314 | 2012-09-07 | `lim2025:conservative_14314` | Hwang Woo-yea (party leader): appointment ceremony of the Digital Party Committee |
| 14447 | 2012-09-25 | `lim2025:conservative_14447` | Commentary on a former minister's suitability to chair a candidate's inter-Korean economic committee |
| 25167 | 2019-05-16 | `lim2025:conservative_25167` | Commentary by the spokesperson on the government's talk of "the future" |

## 2. What was *not* taken from Lim (2025)

- **Democratic Party of Korea dates.** On the current website 7,884 posts carry the placeholder date 2003-11-11. We restored real dates for 7,365 of them **from the signature line in the post text itself**, not from Lim (2025). Lim's corpus was used only to *check* this: for the 6,115 repaired posts whose title matches a unique row in Lim's corpus, 6,080 (99.4%) have the identical date. Lim's dates have not been copied into our data.
- All other rows of all seven parties come from our own collection.

## 3. Licence and citation

Please cite Lim (2025) together with this dataset if you use the 19 rows. Check the licence terms of the source corpus on its OSF page before redistributing them. (TODO: the maintainer has not yet verified these terms.)
