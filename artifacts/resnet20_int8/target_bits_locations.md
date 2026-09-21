# Target bit locations

- Source: `attack/flipped_bits_summary_exact.json`
- Tensor offsets: `combined_manifest.json`
- Page size: 4096
- Total flips: 70

## Format

`layer_name | onnx_tensor | byte | bit | direction | global_byte | page | off`

- `global_byte` = byte offset in `combined_weights.bin`
- `page` = `global_byte // 4096`
- `off` = `global_byte % 4096`

```text
stage_1.2.conv_b.weight | onnx::Conv_269 | 1284 | 0 | 1->0 | 13236 | 3 | 948
conv_1_3x3.weight | onnx::Conv_263 | 171 | 6 | 0->1 | 171 | 0 | 171
conv_1_3x3.weight | onnx::Conv_263 | 212 | 6 | 0->1 | 212 | 0 | 212
stage_1.1.conv_a.weight | onnx::Conv_266 | 179 | 6 | 0->1 | 5219 | 1 | 1123
stage_1.2.conv_a.weight | onnx::Conv_268 | 1639 | 6 | 1->0 | 11287 | 2 | 3095
stage_1.0.conv_a.weight | onnx::Conv_264 | 8 | 6 | 0->1 | 440 | 0 | 440
stage_1.0.conv_a.weight | onnx::Conv_264 | 510 | 2 | 1->0 | 942 | 0 | 942
stage_1.0.conv_a.weight | onnx::Conv_264 | 891 | 6 | 0->1 | 1323 | 0 | 1323
stage_1.0.conv_a.weight | onnx::Conv_264 | 2078 | 7 | 1->0 | 2510 | 0 | 2510
stage_2.1.conv_a.weight | onnx::Conv_272 | 920 | 6 | 0->1 | 29000 | 7 | 328
stage_2.1.conv_a.weight | onnx::Conv_272 | 1166 | 6 | 0->1 | 29246 | 7 | 574
stage_2.1.conv_a.weight | onnx::Conv_272 | 1432 | 3 | 0->1 | 29512 | 7 | 840
stage_2.1.conv_a.weight | onnx::Conv_272 | 1512 | 1 | 1->0 | 29592 | 7 | 920
stage_2.1.conv_a.weight | onnx::Conv_272 | 3694 | 4 | 0->1 | 31774 | 7 | 3102
stage_2.1.conv_a.weight | onnx::Conv_272 | 5918 | 1 | 0->1 | 33998 | 8 | 1230
stage_2.1.conv_a.weight | onnx::Conv_272 | 7044 | 0 | 1->0 | 35124 | 8 | 2356
stage_2.1.conv_a.weight | onnx::Conv_272 | 7240 | 1 | 0->1 | 35320 | 8 | 2552
stage_2.1.conv_a.weight | onnx::Conv_272 | 7787 | 6 | 0->1 | 35867 | 8 | 3099
stage_2.1.conv_a.weight | onnx::Conv_272 | 8122 | 3 | 0->1 | 36202 | 8 | 3434
stage_2.0.conv_a.weight | onnx::Conv_270 | 588 | 6 | 0->1 | 14844 | 3 | 2556
stage_2.0.conv_a.weight | onnx::Conv_270 | 914 | 7 | 0->1 | 15170 | 3 | 2882
stage_2.0.conv_a.weight | onnx::Conv_270 | 1809 | 3 | 1->0 | 16065 | 3 | 3777
stage_2.0.conv_a.weight | onnx::Conv_270 | 2908 | 3 | 1->0 | 17164 | 4 | 780
stage_2.0.conv_a.weight | onnx::Conv_270 | 3200 | 6 | 0->1 | 17456 | 4 | 1072
stage_2.0.conv_a.weight | onnx::Conv_270 | 4087 | 7 | 1->0 | 18343 | 4 | 1959
stage_2.2.conv_a.weight | onnx::Conv_274 | 811 | 7 | 0->1 | 47323 | 11 | 2267
stage_2.2.conv_a.weight | onnx::Conv_274 | 1201 | 6 | 1->0 | 47713 | 11 | 2657
stage_2.2.conv_a.weight | onnx::Conv_274 | 1855 | 3 | 1->0 | 48367 | 11 | 3311
stage_2.2.conv_a.weight | onnx::Conv_274 | 2421 | 6 | 0->1 | 48933 | 11 | 3877
stage_2.2.conv_a.weight | onnx::Conv_274 | 4408 | 6 | 0->1 | 50920 | 12 | 1768
stage_2.2.conv_a.weight | onnx::Conv_274 | 4570 | 4 | 0->1 | 51082 | 12 | 1930
stage_2.2.conv_a.weight | onnx::Conv_274 | 4803 | 7 | 0->1 | 51315 | 12 | 2163
stage_2.2.conv_a.weight | onnx::Conv_274 | 4937 | 7 | 1->0 | 51449 | 12 | 2297
stage_2.2.conv_a.weight | onnx::Conv_274 | 5651 | 2 | 1->0 | 52163 | 12 | 3011
stage_2.2.conv_a.weight | onnx::Conv_274 | 7123 | 7 | 0->1 | 53635 | 13 | 387
stage_1.1.conv_b.weight | onnx::Conv_267 | 393 | 2 | 1->0 | 7737 | 1 | 3641
stage_1.1.conv_b.weight | onnx::Conv_267 | 505 | 7 | 0->1 | 7849 | 1 | 3753
stage_1.1.conv_b.weight | onnx::Conv_267 | 524 | 6 | 0->1 | 7868 | 1 | 3772
stage_1.1.conv_b.weight | onnx::Conv_267 | 1318 | 7 | 1->0 | 8662 | 2 | 470
stage_1.1.conv_b.weight | onnx::Conv_267 | 1592 | 2 | 0->1 | 8936 | 2 | 744
stage_1.1.conv_b.weight | onnx::Conv_267 | 2263 | 0 | 0->1 | 9607 | 2 | 1415
stage_1.0.conv_b.weight | onnx::Conv_265 | 1715 | 6 | 0->1 | 4451 | 1 | 355
stage_2.0.conv_b.weight | onnx::Conv_271 | 5942 | 6 | 0->1 | 24806 | 6 | 230
stage_2.0.conv_b.weight | onnx::Conv_271 | 186 | 7 | 1->0 | 19050 | 4 | 2666
stage_2.0.conv_b.weight | onnx::Conv_271 | 747 | 6 | 0->1 | 19611 | 4 | 3227
stage_2.0.conv_b.weight | onnx::Conv_271 | 2735 | 4 | 0->1 | 21599 | 5 | 1119
stage_2.0.conv_b.weight | onnx::Conv_271 | 3159 | 2 | 1->0 | 22023 | 5 | 1543
stage_2.0.conv_b.weight | onnx::Conv_271 | 3869 | 7 | 1->0 | 22733 | 5 | 2253
stage_2.1.conv_b.weight | onnx::Conv_273 | 4525 | 0 | 1->0 | 41821 | 10 | 861
stage_2.1.conv_b.weight | onnx::Conv_273 | 4651 | 1 | 1->0 | 41947 | 10 | 987
stage_2.1.conv_b.weight | onnx::Conv_273 | 5726 | 1 | 0->1 | 43022 | 10 | 2062
stage_2.1.conv_b.weight | onnx::Conv_273 | 5919 | 7 | 1->0 | 43215 | 10 | 2255
stage_2.1.conv_b.weight | onnx::Conv_273 | 6786 | 7 | 0->1 | 44082 | 10 | 3122
stage_2.1.conv_b.weight | onnx::Conv_273 | 7667 | 6 | 1->0 | 44963 | 10 | 4003
stage_2.1.conv_b.weight | onnx::Conv_273 | 505 | 6 | 0->1 | 37801 | 9 | 937
stage_2.1.conv_b.weight | onnx::Conv_273 | 1269 | 5 | 1->0 | 38565 | 9 | 1701
stage_2.1.conv_b.weight | onnx::Conv_273 | 2273 | 4 | 1->0 | 39569 | 9 | 2705
stage_2.1.conv_b.weight | onnx::Conv_273 | 3081 | 1 | 1->0 | 40377 | 9 | 3513
stage_2.2.conv_b.weight | onnx::Conv_275 | 1827 | 4 | 1->0 | 57555 | 14 | 211
stage_2.2.conv_b.weight | onnx::Conv_275 | 2770 | 6 | 0->1 | 58498 | 14 | 1154
stage_2.2.conv_b.weight | onnx::Conv_275 | 8297 | 4 | 0->1 | 64025 | 15 | 2585
stage_2.2.conv_b.weight | onnx::Conv_275 | 8320 | 7 | 0->1 | 64048 | 15 | 2608
stage_2.2.conv_b.weight | onnx::Conv_275 | 8860 | 6 | 0->1 | 64588 | 15 | 3148
stage_2.2.conv_b.weight | onnx::Conv_275 | 9117 | 7 | 0->1 | 64845 | 15 | 3405
stage_2.2.conv_b.weight | onnx::Conv_275 | 9205 | 5 | 1->0 | 64933 | 15 | 3493
stage_3.0.conv_a.weight | onnx::Conv_276 | 116 | 6 | 0->1 | 65060 | 15 | 3620
stage_3.2.conv_a.weight | onnx::Conv_280 | 967 | 5 | 1->0 | 194935 | 47 | 2423
stage_3.2.conv_a.weight | onnx::Conv_280 | 2389 | 7 | 1->0 | 196357 | 47 | 3845
stage_3.2.conv_a.weight | onnx::Conv_280 | 3529 | 1 | 0->1 | 197497 | 48 | 889
stage_3.2.conv_a.weight | onnx::Conv_280 | 3544 | 6 | 0->1 | 197512 | 48 | 904
```
