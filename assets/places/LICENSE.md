# Generated food images

The 259 JPEGs in this directory (the original set, the 93 listed below and the 78 place covers of the third batch) are synthetic product-style images of food, drink and errand items
(no real products, no text, no logos; see "Brand look-alike products"). They are placeholders for menu items that have no real photo.

- Model: Tongyi-MAI/Z-Image-Turbo (https://huggingface.co/Tongyi-MAI/Z-Image-Turbo)
- Model licence: Apache License 2.0 (per the model card on the Hugging Face Hub, tag `license:apache-2.0`)
- Generated through the Hugging Face `mcp-tools/Z-Image-Turbo` Space API (`/generate`),
  1024x1024, 8 steps, random_seed off; one prompt per file naming the item, with a dark
  charcoal neutral background and soft studio lighting.
- Seeds: 1-9 and 100-148 for the original 58 broad-category images; 149-180 for the 30
  specific-product images added so that distinct products within one sample-menu kind have
  distinct images (seeds 179-180 replaced two first outputs that contained text on a label).
  Accepted key/seed mappings are recorded below; each prompt named the depicted item.
- Post-processing: centre-cropped square, resized to 320x320, saved as baseline JPEG
  (every file is 30 KB or smaller).

Apache-2.0 places no restriction on using the outputs. The images are dark-background stand-ins and are
labelled in the UI as "Sample Only" whenever they appear next to a sample (non-real) menu.
File names are the keys in `src/driveos/images/matcher.py`.


## Second batch (93 images)

Generated with the same model and settings (Tongyi-MAI/Z-Image-Turbo, Apache-2.0), but each image was requested
through the Hugging Face MCP tool for that model (`mcp-tools/Z-Image-Turbo`, `/generate`): 1024x1024, 8 steps,
random_seed off, one fixed seed per file. Post-processing is the same (centre-cropped square, 320x320 baseline JPEG,
every file 30 KB or smaller). Accepted key/seed mappings are recorded below. Prompts end with a dark charcoal neutral
background style and "no text, no labels, no logos,
no words" (covers use a dark moody-tones scene style). Every output was looked at; images with legible text, a
logo or a person were regenerated with another seed and the first output was discarded (seeds 340-353 replace
206, 207, 215, 216, 301, 303, 305, 311, 312, 315, 316, 317, 320 and 348).

Groups:

* 34 dishes and cuisines (pad thai, pho, gyro, poke bowl, bibimbap, falafel, tiramisu, boba tea, dumplings and more).
* 14 alternates (`latte_2`, `cappuccino_2`, `americano_2`, `croissant_2`, `cookie_2`, `cake_2`, `burrito_2`,
  `salad_2`, `beer_2`, `sandwich_2`, `burger_2`, `tenders`, `iced_tea_glass`, `soup_2`) so one keyword does not
  always show one picture.
* 23 brand look-alike products, and 21 place covers (cafe, restaurant, bar, hotel, pharmacy, EV charging, fuel,
  grocery, bakery, fast food, deli).

### Brand look-alike products

Menu items such as Coca-Cola, Diet Coke, Red Bull, Lay's or Advil are shown with these pictures. The pictures are
generic look-alikes: plain coloured cans, bottles, bags and bars with no text, no logo and no trademarked
packaging, and no real product is depicted or implied. The brand appears only as text in the item or option name.
Two first outputs that reproduced a real brand's logo and lettering (seeds 206 and 207) were rejected and replaced
by plain-band cans (seeds 340 and 341). The images are labelled "Sample Only" in the UI like the rest.

Seeds (key, seed):

| key | seed |
|---|---|
| `cola_can` | 200 |
| `diet_cola_can` | 201 |
| `zero_cola_can` | 202 |
| `lemon_lime_can` | 203 |
| `blue_cola_can` | 204 |
| `maroon_soda_can` | 205 |
| `energy_slim_can` | 340 |
| `energy_black_can` | 341 |
| `energy_white_can` | 208 |
| `water_blue_bottle` | 209 |
| `water_tall_bottle` | 210 |
| `water_square_bottle` | 211 |
| `sparkling_can` | 212 |
| `tea_can` | 213 |
| `tea_bottle` | 214 |
| `chips_yellow_bag` | 342 |
| `chips_orange_bag` | 343 |
| `chips_kettle_bag` | 217 |
| `candy_peanut_bar` | 218 |
| `candy_wafer_bar` | 219 |
| `candy_cookie_bar` | 220 |
| `ibuprofen_generic` | 221 |
| `ibuprofen_brand` | 222 |
| `pad_thai` | 230 |
| `pho` | 231 |
| `gyro` | 232 |
| `poke_bowl` | 233 |
| `bibimbap` | 234 |
| `falafel` | 235 |
| `tiramisu` | 236 |
| `boba_tea` | 237 |
| `dumplings` | 238 |
| `fried_rice` | 239 |
| `hummus` | 240 |
| `pancakes` | 241 |
| `egg_sandwich` | 242 |
| `hot_dog` | 243 |
| `mac_cheese` | 244 |
| `grilled_cheese` | 245 |
| `baklava` | 246 |
| `indian_dessert` | 247 |
| `spring_rolls` | 248 |
| `shrimp_plate` | 249 |
| `grilled_salmon` | 250 |
| `oatmeal` | 251 |
| `parfait` | 252 |
| `tamales` | 253 |
| `guacamole` | 254 |
| `caesar_salad` | 255 |
| `avocado_toast` | 256 |
| `lemonade` | 257 |
| `agua_fresca` | 258 |
| `breakfast_plate` | 259 |
| `fruit_plate` | 260 |
| `lo_mein` | 261 |
| `chinese_chicken` | 262 |
| `affogato` | 263 |
| `cheesecake` | 264 |
| `latte_2` | 280 |
| `cappuccino_2` | 281 |
| `americano_2` | 282 |
| `croissant_2` | 283 |
| `cookie_2` | 284 |
| `cake_2` | 285 |
| `burrito_2` | 286 |
| `salad_2` | 287 |
| `beer_2` | 288 |
| `sandwich_2` | 289 |
| `burger_2` | 290 |
| `tenders` | 291 |
| `iced_tea_glass` | 292 |
| `soup_2` | 293 |
| `cover_cafe_1` | 300 |
| `cover_cafe_2` | 344 |
| `cover_restaurant_1` | 302 |
| `cover_restaurant_2` | 345 |
| `cover_restaurant_3` | 304 |
| `cover_bar_1` | 346 |
| `cover_bar_2` | 306 |
| `cover_hotel_1` | 307 |
| `cover_hotel_2` | 308 |
| `cover_pharmacy_1` | 309 |
| `cover_pharmacy_2` | 310 |
| `cover_ev_1` | 347 |
| `cover_ev_2` | 353 |
| `cover_fuel_1` | 313 |
| `cover_fuel_2` | 314 |
| `cover_grocery_1` | 350 |
| `cover_grocery_2` | 349 |
| `cover_bakery_1` | 351 |
| `cover_fastfood_1` | 318 |
| `cover_fastfood_2` | 319 |
| `cover_deli_1` | 352 |

## Third batch (78 place covers)

Place covers so that no list of nearby places repeats a picture: every category now has a pool of at least eight
clearly different scenes (coffee, tea and boba, bakery, sandwich and deli, restaurant, bar and pub and wine, gas
station, EV charging, pharmacy, grocery and store, hotel) plus nine cuisine covers for restaurants (sushi, Thai, Indian,
pizza, Mexican, Mediterranean, ice cream, salad, fried chicken; burger reuses `cover_fastfood_1`).

Same model and settings as the second batch (Tongyi-MAI/Z-Image-Turbo, Apache-2.0, through the Hugging Face MCP tool
`mcp-tools/Z-Image-Turbo`, 1024x1024, 8 steps, random_seed off, one fixed seed per file), dark moody neutral
tones, no text, no logos, no brand trade dress, no people. Post-processing as before (centre crop, 320x320 baseline JPEG,
every file 30 KB or smaller). Accepted key/seed mappings are recorded below.
Every output was looked at; first outputs that showed legible text, a logo, a person or the wrong scene were discarded
and the key was regenerated with another seed (seeds 600-628, the "replaces" rows) and a stricter prompt. One output
(`cover_bakery_2`) is cropped to drop a stray placard at the edge.

| key | seed |
|---|---|
| `cover_cafe_3` | 500 |
| `cover_cafe_4` | 501 |
| `cover_cafe_5` | 502 |
| `cover_cafe_6` | 503 |
| `cover_cafe_7` | 504 |
| `cover_cafe_8` | 505 |
| `cover_tea_1` | 506 |
| `cover_tea_2` | 507 |
| `cover_tea_3` | 508 |
| `cover_tea_4` | 509 |
| `cover_tea_5` | 510 |
| `cover_tea_6` | 511 |
| `cover_tea_7` | 512 |
| `cover_tea_8` | 513 |
| `cover_bakery_2` | 612 (replaces an earlier output) (cropped) |
| `cover_bakery_3` | 515 |
| `cover_bakery_4` | 516 |
| `cover_bakery_5` | 517 |
| `cover_bakery_6` | 613 (replaces an earlier output) |
| `cover_bakery_7` | 519 |
| `cover_bakery_8` | 520 |
| `cover_deli_2` | 521 |
| `cover_deli_3` | 522 |
| `cover_deli_4` | 602 (replaces an earlier output) |
| `cover_deli_5` | 524 |
| `cover_deli_6` | 525 |
| `cover_deli_7` | 526 |
| `cover_deli_8` | 527 |
| `cover_rest_diner` | 603 (replaces an earlier output) |
| `cover_rest_fine` | 529 |
| `cover_rest_patio` | 530 |
| `cover_rest_kitchen` | 604 (replaces an earlier output) |
| `cover_rest_booth` | 532 |
| `cover_rest_sushi` | 533 |
| `cover_rest_thai` | 534 |
| `cover_rest_indian` | 535 |
| `cover_rest_pizza` | 536 |
| `cover_rest_mexican` | 537 |
| `cover_rest_med` | 538 |
| `cover_rest_icecream` | 605 (replaces an earlier output) |
| `cover_rest_salad` | 540 |
| `cover_rest_chicken` | 541 |
| `cover_bar_3` | 614 (replaces an earlier output) |
| `cover_bar_4` | 543 |
| `cover_bar_5` | 607 (replaces an earlier output) |
| `cover_bar_6` | 608 (replaces an earlier output) |
| `cover_bar_7` | 609 (replaces an earlier output) |
| `cover_bar_8` | 547 |
| `cover_fuel_3` | 610 (replaces an earlier output) |
| `cover_fuel_4` | 611 (replaces an earlier output) |
| `cover_fuel_5` | 615 (replaces an earlier output) |
| `cover_fuel_6` | 616 (replaces an earlier output) |
| `cover_fuel_7` | 552 |
| `cover_fuel_8` | 553 |
| `cover_ev_3` | 617 (replaces an earlier output) |
| `cover_ev_4` | 618 (replaces an earlier output) |
| `cover_ev_5` | 619 (replaces an earlier output) |
| `cover_ev_6` | 620 (replaces an earlier output) |
| `cover_ev_7` | 558 |
| `cover_ev_8` | 625 (replaces an earlier output) |
| `cover_pharmacy_3` | 560 |
| `cover_pharmacy_4` | 561 |
| `cover_pharmacy_5` | 626 (replaces an earlier output) |
| `cover_pharmacy_6` | 623 (replaces an earlier output) |
| `cover_pharmacy_7` | 564 |
| `cover_pharmacy_8` | 565 |
| `cover_grocery_3` | 624 (replaces an earlier output) |
| `cover_grocery_4` | 567 |
| `cover_grocery_5` | 627 (replaces an earlier output) |
| `cover_grocery_6` | 569 |
| `cover_grocery_7` | 570 |
| `cover_grocery_8` | 628 (replaces an earlier output) |
| `cover_hotel_3` | 572 |
| `cover_hotel_4` | 573 |
| `cover_hotel_5` | 574 |
| `cover_hotel_6` | 575 |
| `cover_hotel_7` | 576 |
| `cover_hotel_8` | 577 |
