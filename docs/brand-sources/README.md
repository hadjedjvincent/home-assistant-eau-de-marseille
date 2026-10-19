# Brand image sources and maintenance

The assets were checked on October 14, 2026, against the provider's official sources:

- [Vector logo](https://www.eaudemarseille-metropole.fr/app/uploads/sites/2/2022/11/logo-metropole.svg).
- [Website icon](https://www.eaudemarseille-metropole.fr/app/uploads/sites/2/2022/12/cropped-favicon-eaux-de-marseille-144x144-v5-270x270.png).
- [Supported customer portal](https://espaceclients.eaudemarseille-metropole.fr).

The images identify Eau de Marseille Métropole, the service supported by this
integration. The logo is rasterized from the official SVG, and the icon is resized
from the official favicon. The artwork has not been redrawn or generated.

The high-resolution icon comes from a 270-pixel source. Resizing meets the required
output dimensions but cannot add detail to the original image.

| File | Dimensions | Source |
| --- | --- | --- |
| `custom_components/sem/brand/icon.png` | 256 × 256 | Official favicon |
| `custom_components/sem/brand/icon@2x.png` | 512 × 512 | Official favicon |
| `custom_components/sem/brand/logo.png` | 507 × 256 | Official SVG |
| `custom_components/sem/brand/logo@2x.png` | 1013 × 512 | Official SVG |

The optimized PNG files retain transparency and are trimmed around the artwork.
The logo preserves the official white areas rather than converting them to
transparency. The assets have been reviewed on light and dark backgrounds.

Home Assistant supports local `custom_components/sem/brand/` assets starting with version 2026.3.

The marks and artwork remain the property of their owners. They identify the
supported service and are excluded from the code's MIT license. This integration
is independent and is not an official or provider-endorsed product.

See the [image specifications](https://github.com/home-assistant/brands#image-specification)
and [Home Assistant brand image documentation](https://developers.home-assistant.io/docs/core/integration/brand_images/).
