"""
Functions for finding pairs within a certain search distance "err".

Very fast method based on hashing.
"""

import numpy
import itertools
import os, sys
import astropy.io.fits as pyfits
import progressbar
from numpy import sin, cos, arccos, arcsin, pi, exp, log
import joblib
import os
cachedir = 'cache'
if not os.path.isdir(cachedir): os.mkdir(cachedir)
mem = joblib.Memory(cachedir=cachedir, verbose=False)

"""
From topcat: Haversine formula for spherical trigonometry.
     * This does not have the numerical instabilities of the cosine formula
     * at small angles.
     * <p>
     * This implementation derives from Bob Chamberlain's contribution
     * to the comp.infosystems.gis FAQ; he cites
     * R.W.Sinnott, "Virtues of the Haversine", Sky and Telescope vol.68,
     * no.2, 1984, p159.
     *
     * @see  <http://www.census.gov/geo/www/gis-faq.txt>
currently hosted at http://www.ciesin.org/gisfaq/gis-faq.txt
"""
def dist((a_ra, a_dec), (b_ra, b_dec)):
	ra1 = a_ra / 180 * pi
	dec1 = a_dec / 180 * pi
	ra2 = b_ra / 180 * pi
	dec2 = b_dec / 180 * pi
        sd2 = sin(0.5 * (dec2 - dec1))
        sr2 = sin(0.5 * (ra2 - ra1))
        a = sd2**2 + sr2**2 * cos(dec1) * cos(dec2);
        return numpy.where(a < 1.0, 2.0 * arcsin(a**0.5), pi) * 180 / pi;

def get_tablekeys(table, name):
	keys = sorted(table.dtype.names, key=lambda k: 0 if k.upper() == name else 1 if k.upper().startswith(name) else 2)
	assert len(keys) > 0 and name in keys[0].upper(), 'ERROR: No "%s"  column found in input catalogue. Only have: %s' % (name, ', '.join(table.dtype.names))
	return keys[0]

@mem.cache
def crossproduct(radectables, err):
	buckets = {}
	pbar = progressbar.ProgressBar(widgets=[
		progressbar.Percentage(), progressbar.Counter('%3d'),
		progressbar.Bar(), progressbar.ETA()], maxval=sum([len(t[0]) for t in radectables])).start()
	for ti, (ra_table, dec_table) in enumerate(radectables):
		for ei, (ra, dec) in enumerate(zip(ra_table, dec_table)):
			i, j = int(ra / err), int(dec / err)
			
			# put in bucket, and neighbors
			for jj, ii in (j,i), (j,i+1), (j+1,i), (j+1, i+1):
				k = (ii, jj)
				if k not in buckets: # prepare bucket
					buckets[k] = [[] for _ in range(len(radectables))]
				buckets[k][ti].append(ei)
			pbar.update(pbar.currval + 1)
	pbar.finish()
	
	# add no-counterpart options
	results = set()
	# now combine within buckets
	print 'matching: %6d matches after hashing' % numpy.sum([
		len(lists[0]) * numpy.product([len(li) + 1 for li in lists[1:]]) 
			for lists in buckets.values()])

	print 'matching: collecting from %d buckets' % len(buckets)
	pbar = progressbar.ProgressBar(widgets=[
		progressbar.Percentage(), progressbar.Counter('%3d'), 
		progressbar.Bar(), progressbar.ETA()], maxval=len(buckets)).start()
	while buckets:
		k, lists = buckets.popitem()
		if len(lists[0]) == 0:
			continue
		for l in lists[1:]:
			l.append(-1)
		comb = itertools.product(*lists)
		results.update(comb)
		pbar.update(pbar.currval + 1)
	pbar.finish()

	print 'matching: %6d unique matches from crossproduct' % len(results)
	# now make results unique by sorting
	results = numpy.array(sorted(results))
	
	onlyfirst = (results != -1).sum(axis=1) == 1
	results = results[-onlyfirst]
	
	print 'matching: %6d matches' % len(results)
	return results

def match_multiple(tables, table_names, err, fits_formats):
	"""
	computes the cartesian product of all possible matches,
	limited to a maximum distance of err (in degrees).
	
	tables: input FITS table
	table_names: names of the tables
	fits_formats: FITS data type of the columns of each table
	
	returns 
	results: cartesian product of all possible matches (smaller than err)
	cat_columns: table with separation distances in arcsec
	header: which columns were used in each table for RA/DEC
	
	"""
	
	print 'matching with %f arcsec radius' % (err * 60 * 60)
	print 'matching: %6d naive possibilities' % numpy.product([len(t) for t in tables])

	print 'matching: hashing'

	ra_keys = [get_tablekeys(table, 'RA') for table in tables]
	print '    using RA  columns: %s' % ', '.join(ra_keys)
	dec_keys = [get_tablekeys(table, 'DEC') for table in tables]
	print '    using DEC columns: %s' % ', '.join(dec_keys)
	#ra_min = min([table[k].min() for k, table in zip(ra_keys, tables)])
	#ra_max = max([table[k].max() for k, table in zip(ra_keys, tables)])
	#dec_min = min([table[k].min() for k, table in zip(dec_keys, tables)])
	#dec_max = max([table[k].max() for k, table in zip(dec_keys, tables)])
	#print ra_min, ra_max, dec_min, dec_max

	ratables = [(t[ra_key], t[dec_key]) for t, ra_key, dec_key in zip(tables, ra_keys, dec_keys)]
	resultstable = crossproduct(ratables, err)
	#print results[:100]
	#print results.shape, results.dtype, len(table_names)
	results = resultstable.view(dtype=[(table_name, resultstable.dtype) for table_name in table_names]).reshape((-1,))
	#print results.shape, results.dtype, len(table_names)

	#print results[:100]

	keys = []
	for table_name, table in zip(table_names, tables):
		keys += ["%s_%s" % (table_name, n) for n in table.dtype.names]
	
	print 'merging columns ...', sum([1 + len(table.dtype.names) for table in tables])
	cat_columns = []
	pbar = progressbar.ProgressBar(widgets=[
		progressbar.Percentage(), progressbar.Counter('%3d'), 
		progressbar.Bar(), progressbar.ETA()], 
		maxval=sum([1 + len(table.dtype.names) for table in tables])).start()
	for table, table_name, fits_format in zip(tables, table_names, fits_formats):
		tbl = table[results[table_name]]
		# set missing to nan
		mask_missing = results[table_name] == -1
		#tbl[mask_missing]
		
		pbar.update(pbar.currval + 1)
		for n, format in zip(table.dtype.names, fits_format):
			k = "%s_%s" % (table_name, n)
			keys.append(k)
			col = tbl[n]
			#print '   setting "%s" to -99 (%d affected; column format "%s")' % (k, mask_missing.sum(), format)
			try:
				col[mask_missing] = -99
			except Exception as e:
				print '   setting "%s" to -99 failed (%d affected; column format "%s"): %s' % (k, mask_missing.sum(), format, e)
			
			fitscol = pyfits.Column(name=k, format=format, array=col)
			#if mask_missing.any():
			#	print table_name, n, k, tbl[n][:100], '-->', fitscol.array[:100]
			cat_columns.append(fitscol)
			pbar.update(pbar.currval + 1)
	pbar.finish()
	
	tbhdu = pyfits.BinTableHDU.from_columns(pyfits.ColDefs(cat_columns))
	header = dict(
		COLS_RA = ' '.join(["%s_%s" % (ti, ra_key) for ti, ra_key in zip(table_names, ra_keys)]),
		COLS_DEC = ' '.join(["%s_%s" % (ti, dec_key) for ti, dec_key in zip(table_names, dec_keys)])
	)
	
	print 'merging columns: adding angular separation columns'
	cols = []
	max_separation = numpy.zeros(len(results))
	for i in range(len(tables)):
		a_ra  = tbhdu.data["%s_%s" % (table_names[i], ra_keys[i])]
		a_dec = tbhdu.data["%s_%s" % (table_names[i], dec_keys[i])]
		for j in range(i):
			k = "Separation_%s_%s" % (table_names[i], table_names[j])
			keys.append(k)
			
			b_ra  = tbhdu.data["%s_%s" % (table_names[j], ra_keys[j])]
			b_dec = tbhdu.data["%s_%s" % (table_names[j], dec_keys[j])]
			
			col = dist((a_ra, a_dec), (b_ra, b_dec))
			assert not numpy.isnan(col).any(), ['%d distances are nan' % numpy.isnan(col).sum(), 
				a_ra[numpy.isnan(col)], a_dec[numpy.isnan(col)], 
				b_ra[numpy.isnan(col)], b_dec[numpy.isnan(col)]]
			
			col[a_ra == -99] = numpy.nan
			col[b_ra == -99] = numpy.nan
			max_separation = numpy.nanmax([col * 60 * 60, max_separation], axis=0)
			# store distance in arcsec 
			cat_columns.append(pyfits.Column(name=k, format='E', array=col * 60 * 60))
	
	cat_columns.append(pyfits.Column(name="Separation_max", format='E', array=max_separation))
	cat_columns.append(pyfits.Column(name="ncat", format='I', array=(resultstable > -1).sum(axis=1)))
	keys.append("Separation_max")
	mask = max_separation < err * 60 * 60
	for c in cat_columns:
		c.array = c.array[mask]
	
	print 'matching: %6d matches after filtering' % mask.sum()
	
	return results[mask], cat_columns, header

def wraptable2fits(cat_columns, extname):
	tbhdu = pyfits.BinTableHDU.from_columns(pyfits.ColDefs(cat_columns))
	hdu = pyfits.PrimaryHDU()
	import datetime, time
	now = datetime.datetime.fromtimestamp(time.time())
	nowstr = now.isoformat()
	nowstr = nowstr[:nowstr.rfind('.')]
	#hdu.header['CREATOR'] = """Johannes Buchner <johannes.buchner.acad@gmx.com>"""
	hdu.header['DATE'] = nowstr
	hdu.header['ANALYSIS'] = '3WAY matching'
	tbhdu.header['EXTNAME'] = extname
	hdulist = pyfits.HDUList([hdu, tbhdu])
	return hdulist

def array2fits(table, extname):
	cat_columns = pyfits.ColDefs([pyfits.Column(name=n, format='E',array=table[n]) 
		for n in table.dtype.names])
	return wraptable2fits(cat_columns, extname)

if __name__ == '__main__':
	filenames = sys.argv[1:]
	
	numpy.random.seed(0)

	def gen():
		ra  = numpy.random.uniform(size=1000)
		dec = numpy.random.uniform(size=1000)
		return numpy.array(zip(ra, dec), dtype=[('ra', 'f'), ('dec', 'f')])

	for fitsname in filenames:
		if os.path.exists(fitsname):
			continue
		print 'generating toy data for %s!' % fitsname
		hdulist = array2fits(gen(), fitsname.replace('.fits', ''))
		hdulist[0].header['GENERAT'] = 'match test table, random'
		hdulist.writeto(fitsname, clobber=False)
	
	tables = [pyfits.open(fitsname)[1] for fitsname in filenames]
	table_names = [t.name for t in tables]
	tables = [t.data for t in tables]
	
	err = 0.03
	
	results, columns = match_multiple(tables, table_names, err)
	tbhdu = pyfits.BinTableHDU.from_columns(pyfits.ColDefs(columns))
	
	hdulist = wraptable2fits(tbhdu, 'MATCH')
	hdulist[0].header['ANALYSIS'] = 'match table from' + ', '.join(table_names)
	hdulist[0].header['INPUT'] = ', '.join(filenames)
	hdulist.writeto('match.fits', clobber=True)

	print 'plotting'
	import matplotlib.pyplot as plt
	for table_name in table_names:
		plt.plot(tbhdu.data['%s_RA'  % table_name], 
			 tbhdu.data['%s_DEC' % table_name], '.')
	
	for r in tbhdu.data:
		plt.plot(r['%s_RA'  % table_name],
			 r['%s_DEC' % table_name],
			'-', color='b', alpha=0.3)
	
	plt.savefig('matchtest.pdf')



def test_dist():
	
	d = dist((53.15964508, -27.92927742), (53.15953445, -27.9313736))
	print 'distance', d
	assert not numpy.isnan(d)
	
	ra = numpy.array([ 53.14784241,  53.14784241,  53.14749908, 53.16559982,     53.19423676,  53.1336441 ])
	dec = numpy.array([-27.79363823, -27.79363823, -27.81790352, -27.79622459,      -27.70860672, -27.76327515])
	ra2 = numpy.array([ 53.14907837,  53.14907837,  53.1498642 , 53.16150284,     53.19681549,  53.13626862])
	dec2 = numpy.array([-27.79297447, -27.79297447, -27.81404877, -27.79223251,  -27.71365929, -27.76314735])

	d = dist((ra, dec), (ra2, dec2))
	print 'distance', d
	assert not numpy.isnan(d).any()
	
