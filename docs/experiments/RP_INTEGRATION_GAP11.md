# RP integration: F150/r433 11-question gap case

[简体中文](RP_INTEGRATION_GAP11.zh-CN.md)

This case records a frozen real-context gap test for `writer-B`: Qwen dual-path recall and the Decision v6 control flow were used to retrieve needed material for 11 questions. Aggregated data is stored in [fated-poem-gap11-20261031.json](fated-poem-gap11-20261031.json).

## Reported results

Material sufficiency and use in the final answer are separate outcomes:

| Method | Questions with complete material / 11 | Required facts complete / 15 |
| --- | ---: | ---: |
| Qwen dual-path recall + Decision v6 | 11 | 15 |
| `pi-rp` control | 6 | 9 |
| Original Agent control | 7 | 10 |
| context-only baseline | 0 | 0 |
| Early in-house language-controller record | 10 | 14 |

Final answers were separately recorded as 10 complete and 1 partial; this does not replace the material-recall score. The cost of Decision v6 is unknown.

## Scope of the conclusion

This is an integration case using real RP context and a frozen set of 11 questions. It supports the limited conclusion that Qwen dual-path recall plus Decision v6 outperformed the listed measured controls on material sufficiency in this case. It does not by itself prove that pure embedding retrieval outperforms other retrievers, establish general performance gains, or prove isolation across different actors or worldlines from a single case. The contributions of Decision v6 and Qwen material recall should be interpreted separately.
