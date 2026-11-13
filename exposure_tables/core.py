from pathlib import Path
from astropy.io import fits


def inspect_file(path):
    with fits.open(Path(path),memmap=True,lazy_load_hdus=True) as hdus:
        return {'hdus':[{'index':index,'name':hdu.name,'extver':hdu.header.get('EXTVER'),
                         'kind':type(hdu).__name__,'rows':hdu.header.get('NAXIS2')} for index,hdu in enumerate(hdus)]}
